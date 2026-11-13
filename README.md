# 报关单自动录入系统

替报关行把客户 Excel / PDF 发票里的数据自动录进海关申报系统，并保证**同一票永不重复申报**。

包含三部分：

| 组件 | 说明 | 端口 |
|---|---|---|
| `mock_system/` | 模拟海关申报系统（登录、贸易方式联动字段、明细分页、会话过期、随机退单） | 5055 |
| `app/` | 报关行控制台 + Playwright 自动录入引擎 + SQLite | 5050 |
| `common/` | 两边共享的贸易方式、HS 目录、申报要素规则、字段标签 | — |

---

## 快速开始

```bash
# 1. 依赖（已安装可跳过）
pip install -r requirements.txt
python -m playwright install chromium

# 2. 一键初始化（建库 + 生成测试发票）
bash scripts/setup.sh

# 3. 同时启动模拟海关系统与控制台
bash scripts/dev.sh
```

打开 <http://127.0.0.1:5050>，把 `data/samples/` 下的 Excel/PDF 拖进「待录队列」页即可。

模拟海关系统：<http://127.0.0.1:5055>，账号 `admin / admin123`。

---

## 功能对照

| 需求 | 实现 |
|---|---|
| 导入 Excel / PDF 发票 | `app/importer/`：Excel 扁平表与表头块两种版式；PDF 用坐标聚类还原表格 |
| 自动拆票 | 单票超过 20 项自动拆成 `发票号-01/-02…`，备注保留源发票号 |
| HS 编码 / 申报要素先校验 | `validate_invoice()`：失效/未知 HS、法定单位、申报要素缺失与取值、金额勾稽、贸易方式必填项等 |
| 逐票自动录入 | `app/runner/`，Playwright 驱动真实页面 |
| 联动字段等它出来再填 | 等待 `/api/trade-mode/<code>` 响应 **且** DOM 字段数与返回一致后再填 |
| 每步截图留档 | 登录、表头、每条明细、提交前、结果页、回读页；存 `data/screenshots/run_<id>/`，任务页可展开查看 |
| 会话过期自动重登 | 检测 401 / 跳转登录页；重登后重开申报页，从系统草稿的断点明细继续 |
| 回读比对逐字段 | `app/services/reconcile.py`：表头 + 每条明细 + 申报要素逐字段比对，差异标红 |
| 随机退单解析进队列 | 结果页解析退单代码与原因，写入 `rejections`，按类别归集 |
| 退单改完能重录 | 修正后「加入待录队列」，runner 走同一套防重围栏再次申报 |
| 按天统计录入量/成功率 | 录入任务页图表 + `xlsx` 导出（每日统计、单据明细、退单统计三个 sheet） |

---

## 防重复申报：核心设计

> 中途杀进程再起来，也绝不能多交一票。

### 1. 一次性围栏（fence）

`submissions` 表对每票只有一行（`doc_id UNIQUE`），状态机：

```
none ──claim──> submitting ──server accepted──> accepted   (终态，永不再交)
                     │
                     ├──server rejected──> rejected ──(人工修复后)──> claim again
                     │
                     └──结果不明──> uncertain  (禁止自动重交，需人工核实)
```

**提交前必须在同一个 `BEGIN IMMEDIATE` 事务里把 `none → submitting` 并写入认领进程标识**，事务提交成功后才发 POST。并发认领只有一个能成功（测试 `tests/test_fence.py::test_concurrent_claims_exactly_one_wins`）。

### 2. 崩溃恢复靠“证据”，不靠“重试”

进程在提交窗口内被杀后重启：

1. 先向申报系统查询该外部单号（`/api/query`）。
2. 系统**已有受理单** → 直接采纳该报关单号，继续回读核对，**绝不重发**。
3. 系统**有明确退单** → 记为退单，进入退单队列。
4. 系统**两者都没有** → 标记 `uncertain` 锁定，必须由人工核实后才能解除。

### 3. 录入前全量预检

每票录入前都查一次系统：外部门号已存在则跳过。即使本地库丢失/回滚，也不会重复申报。

### 4. 结果不明的人工兜底

核对差异页/单据详情的「人工核实」：确认已受理则登记报关单号；确认未提交才释放围栏允许重录。

---

## 目录结构

```
common/            共享目录与规范化（贸易方式、HS、申报要素、比较规则）
mock_system/       模拟海关系统（Flask）
  app.py           登录/会话、联动字段 API、草稿、提交、退单、查询 API
app/
  db.py            SQLite 架构与事务封装
  importer/        Excel / PDF 解析、拆票、字段校验
  runner/          Playwright 客户端 + 防重 runner（CLI：python -m app.runner.main）
  services/        fence（防重）、state（状态机）、reconcile（回读）、stats（统计导出）
  web/             控制台（待录队列/录入任务/核对差异/退单处理/设置）
scripts/           测试数据生成、启动脚本
tests/             单元 + 端到端测试（含崩溃/会话过期/并发）
data/              运行时生成：broker.db、mock.db、screenshots/、exports/
```

---

## 命令行

```bash
# 生成测试发票
python scripts/make_test_data.py --out data/samples

# 导入（服务已启动时也可用 UI）
curl -F "files=@data/samples/客户发票_华东_20261113.xlsx" http://127.0.0.1:5050/api/import

# 录入全部待录（或指定票）
python -m app.runner.main
python -m app.runner.main --docs 1,2,3
python -m app.runner.main --batch 1 --headless

# 测试
python -m pytest tests/ -v
```

### 测试钩子（仅用于验证崩溃/会话行为）

```bash
# 在“取得提交许可后、发出 POST 前”杀进程
RUNNER_CRASH_POINT=after_claim RUNNER_CRASH_DOC=3 python -m app.runner.main --docs 3

# 在“提交已返回、本地未落定”杀进程（系统已受理）
RUNNER_CRASH_POINT=after_submit_response RUNNER_CRASH_DOC=3 python -m app.runner.main --docs 3

# 录到第 2 条明细后强制会话过期，验证自动重登续录
RUNNER_EXPIRE_AFTER_ITEM=2 python -m app.runner.main --docs 3
```

---

## 配置

控制台「设置」页可改：申报系统地址、账号密码、会话时限、无头模式、退单重录上限、
提交异常等待、结果核实次数、是否自动回读。

环境变量：

| 变量 | 默认 | 用途 |
|---|---|---|
| `CONSOLE_PORT` | 5050 | 控制台端口 |
| `MOCK_PORT` | 5055 | 模拟系统端口 |
| `MOCK_SESSION_MINUTES` | 20 | 模拟系统会话时长 |
| `MOCK_REJECTION_RATE` | 0.25 | 随机退单概率 |
| `MOCK_SEED` | — | 退单随机种子（便于复现） |
| `BROKER_DATA_DIR` / `BROKER_DB` | `data/` | 数据目录 / 库路径 |

---

## 测试覆盖

`python -m pytest tests/`（51 个用例）：

* `test_parsing.py` — Excel/PDF 解析、拆票、要素还原、校验规则（失效 HS、单位、金额、要素、币制）
* `test_fence.py` — 围栏状态机、并发唯一认领、退单重录上限、崩溃可见性、非法状态迁移
* `test_runner_e2e.py` — 真实 Playwright 跑模拟系统：受理+回读、二次运行不重复、**两种崩溃点**、会话过期重登、退单修复重录
* `test_console.py` — 导入/去重/修正/入队/退单处理/人工核实/导出/路径穿越

端到端用例覆盖的崩溃点：`after_claim`（认领后、POST 前）与
`after_submit_response`（服务端已受理、本地未落定）。两者都验证重启后
**不会再提交**；后者还会自动采纳已受理的报关单并完成回读。

---

## 已知边界

* PDF 仅支持文本型（非扫描件）；扫描件需 OCR，当前会明确报错提示改导 Excel。
* 申报要素按 HS 目录推导必填项；目录见 `common/catalog.py`，接入真实税则时替换该文件即可。
* 模拟系统的随机退单用于演练退单流程；真实环境关闭 `MOCK_REJECTION_RATE`。
* 控制台无登录（本机工具）；多用户部署需在其前面加认证层。

---

## 回读发现差异时怎么办

| 单据状态 | 能否直接重录 | 处理方式 |
|---|---|---|
| `verified_mismatch`（系统已受理但字段不一致） | **否** | 在申报系统对原单做更改/作废；确认后再登记，避免重复申报 |
| `rejected`（系统退单） | 是 | 「退单处理」标记已处理 → 修正数据 → 加入待录队列 |
| `uncertain`（结果不明） | 否 | 单据详情「人工核实」：确认已受理则登记报关单号；确认未提交才释放 |

系统对“已受理”的票一律拒绝再次提交，这是防重复申报的核心约束，不是缺陷。
