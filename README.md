# 报关单自动录入系统

把客户 Excel / PDF 商业发票里的数据，自动录入本地模拟的海关申报系统，逐字段回读核对，
退单自动进队列处理，并按天出统计报表。

技术栈：Python 3.13 + Playwright + SQLite + Flask 网页控制台。全部本地运行，无需外网。

```text
┌────────────────┐   ┌─────────────────────┐   ┌─────────────────────┐
│ 客户发票        │   │ 报关控制台            │   │ Mock 海关申报系统     │
│ Excel / PDF    ├──▶│ 导入·队列·核对·退单   ├──▶│ 登录·联动字段·分页·审核 │
└────────────────┘   └──────────┬──────────┘   └──────────▲──────────┘
                                │ SQLite                   │ Playwright
                                ▼                          │
                     ┌─────────────────────────────────────┴──┐
                     │ 录入进程：建单→表头→明细→提交→回读比对   │
                     └────────────────────────────────────────┘
```

## 快速开始

```bash
./scripts/start.sh            # 启动 Mock 申报系统(8099) + 控制台(8088)
./scripts/start.sh --worker   # 同时启动后台录入进程
```

- Mock 申报系统：<http://127.0.0.1:8099>，账号 `demo / demo123`
- 报关控制台：<http://127.0.0.1:8088>
- 演示数据：控制台「待录队列」页直接上传 `samples/` 里的 Excel / PDF；
  或 `python3 scripts/seed_demo.py` 把样例批量导入。
- 停止：`./scripts/stop.sh`

### 一次性演示流程

1. 打开控制台 →「待录队列」→ 选中 `samples/` 下多个文件 → 开始导入。
   报告页会显示每张发票的拆分结果、HS 编码与申报要素校验结论。
2. 「录入任务」→「启动队列录入」。浏览器自动逐票操作 Mock 系统，右侧实时日志滚动，
   每步生成截图（`data/screenshots/`）。
3. 录完自动回读系统数据，「核对差异」页逐字段列出源单值 vs 系统值，不一致标红。
4. Mock 系统按设定概率退单。「退单处理」页显示解析后的分类、定位字段和修复建议，
   「自动修复并重录」会把可推导的字段改好并重新排队。
5. 「统计报表」按天汇总录入量、通过率、退单数，可导出 Excel（三张工作表）。

## 测试数据

`samples/` 由 `scripts/make_samples.py` 生成（固定随机种子，可复现）：

```bash
PYTHONPATH=. python3 scripts/make_samples.py --out samples --count 8 --pdf 3 --split 1
```

- `invoices_7sheets.xlsx`：一张工作簿 7 张发票，每张一个 sheet
- `invoices_multi.xlsx`：**一个 sheet 里竖排两张发票**（验证自动拆票）
- `invoice_CI-*.pdf`：中文商业发票，表格 + 申报要素段落，覆盖 8 种贸易方式
- 覆盖 11 个商品、10 个 HS 编码，含法定第二单位（千克/升）的品类
- `manifest.json`：每张发票的完整结构化数据，测试里用来做黄金对照

## 功能说明

### 1. 导入与校验

| 能力 | 说明 |
| --- | --- |
| Excel | 每个 sheet 一张发票；一个 sheet 多张发票按标题/发票号行自动切分 |
| PDF | `pdfplumber` 表格 + 文本双层解析；申报要素段落按项号回填 |
| 表头识别 | 中英文标签 × 值布局无关（任意列位置都能识别） |
| HS 校验 | 10 位数字、本地税则库命中 |
| 申报要素 | 按税则定义逐项校验必填/枚举/数字类型 |
| 数值校验 | 数量×单价≈总价、法定数量、第二数量、国别代码、单位 |
| 拆票结果 | 写入 `import_batches.report`，控制台展示每张发票的结论 |

校验未通过的票据状态为 `hold`，**不会**进入录入队列，修正后点「重新校验」自动入队。

### 2. 自动录入

`declarant/worker/client.py` 用 Playwright 操作页面：

- 建单前先按客户参考号查系统，**存在就复用，绝不二次建单**
- 贸易方式选中后**等待联动字段真实出现**（`wait_for_function` + 面板可见），再填写
- 输入 HS 后等待申报要素异步加载完成，才继续填其余字段
- 商品明细分页录入，每项保存后截图留档
- 每行保存前比对页面现值与源单，一致则跳过（幂等重放）

### 3. 会话 20 分钟过期

- Mock 侧服务端会话 20 分钟无操作即失效，所有页面重定向登录页
- 驱动每次动作后检测登录页；命中则重登并**重放当前这一步**
- 商品行级别的循环：第 N 行保存时会话掉了 → 重登 → 重新打开明细页 → 重填第 N 行
- 已保存的前 N-1 行不重复提交（`_item_already_saved` 校验 HS/数量/总价/品名）
- 任务 payload 的 `next_seq` 落库，进程崩溃后从该行续录

### 4. 回读核对

录入完成后按 `data-field` 标记回读系统详情页，构造 30+ 表头字段与每项 8 个字段 + 全部申报要素
的比对矩阵，写入 `reconciliation` / `reconciliation_run`。差异页红色高亮，可重新回读比对。

### 5. 退单处理

Mock 退单原因以 `[要素不符]/[价格不符]/[数量不符]/[单证不符]/[表头不符]` 开头并带项号与字段名，
解析器转成 `category / item_seq / field_path / suggestion` 存 `rejections` 表。

「自动修复」只做**可推导**的保守修正：

- 价格不符 → 总价 = 数量 × 单价
- 数量不符 → 法定数量按 1:1 折算（当单位一致时）
- 要素不符 → 非法枚举值回落到税则允许的首个选项
- 单证不符 → 表头发票号按源单重新同步

其他情况提示人工修改。修复后重新排队，**沿用原报关单号**重新录入并提交。

### 6. 不重复申报（重点）

四重防线，任何一环单独失效也不会有第二票：

| 层 | 机制 | 位置 |
| --- | --- | --- |
| 1 | 发票指纹 `UNIQUE`：发票号+贸易方式+商品行摘要，重复导入直接跳过 | `declarations.fingerprint` |
| 2 | 提交台账 `UNIQUE(fingerprint, revision)`：**点提交之前**先写 `intent` 行，进程被杀后重跑看到 intent 就改为查询系统状态 | `submit_ledger` |
| 3 | 系统侧 `UNIQUE(client_ref)`：Mock 建单/提交都拒绝重复，退回原单号 | `mock_system/app.py` |
| 4 | 录前查询：每次提交前按客户参考号查询，已申报/审核通过则跳过提交 | `client.submit()` |

崩溃场景（测试覆盖）：提交按钮点击后 `os._exit(137)` → 重启进程 → 走 intent 恢复分支，
向系统核实状态，不刷新提交按钮。测试断言 Mock 侧 `submit_attempts` 中 `SUBMITTED` 恰好 1 次。

另有 `claim()` 原子抢占（`UPDATE ... WHERE status IN ('queued','validated')`），
多进程/多线程同时跑也只有一个能录同一票；`recover_stale()` 回收崩溃遗留的 `recording` 态。

### 7. 统计与导出

`daily_stats` 记录导入/提交/通过/退单/重复拦截。导出 Excel 含三张表：
每日统计、报关单明细、退单分析。

## 目录结构

```text
declarant/
  common.py              # 配置、DB schema、税则、校验
  importer/parsers.py    # Excel/PDF 解析与归一化
  importer/service.py    # 指纹、入库、批次报告、重新校验
  worker/client.py       # Playwright 驱动（登录/表头/明细/提交/回读）
  worker/engine.py       # 任务队列、提交台账、会话刷新、守护进程
  worker/reconcile.py    # 逐字段比对
  worker/rejections.py    # 退单解析与自动修复
  web/app.py             # 控制台路由
  web/templates/         # 7 个页面 + 详情/编辑/报告
mock_system/             # 本地模拟申报系统（Flask + 原生 JS）
reference/catalog.json   # 税则、贸易方式、国别、币制、商品主数据
samples/                 # 生成的测试发票
scripts/                 # 启动/停止/造数/播种
tests/                   # 25 个测试：解析、导入、并发、崩溃、端到端
```

## 测试

```bash
PYTHONPATH=. python3 -m pytest tests -q          # 全部（约 85 秒，需 Chrome）
PYTHONPATH=. python3 -m pytest tests -q -m "not e2e"   # 只跑不启动浏览器的
```

关键用例：

- `test_import.py` — 解析、拆票、校验、重复导入幂等
- `test_e2e.py` — 完整录入、会话过期续录、**提交后 kill -9 不重复申报**、退单修复重录、
  提交意图超时接管（点提交前被杀 → 只补交一次）
- `test_concurrency.py` — 8 线程抢同一票只有 1 个 claim 成功；崩溃租约回收
- `test_web.py` — 页面渲染、上传导入、编辑→校验→入队、Excel 导出

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DECLARANT_DATA_DIR` | `./data` | SQLite、截图、上传、导出目录 |
| `MOCK_PORT` | `8099` | Mock 申报系统端口 |
| `DECLARANT_PORT` | `8088` | 控制台端口 |
| `MOCK_REJECT_RATE` | `0.2` | 首次提交退单概率（也可在设置页热调） |
| `MOCK_SESSION_TTL_SECONDS` | `1200` | 会话有效期，测试可调小 |
| `MOCK_ALLOW_TEST_ENDPOINTS` | `1` | 是否暴露 `/api/test/*` 演练接口 |

## 已知边界

- 发票版式按「标签-值」和「项号表格」两类常见布局做了通用识别；极端版式仍需扩解析规则。
- 单票录入为串行（并发数默认 1），并发多会话会引入重复申报风险，不建议放开。
- Mock 系统是同步审核（提交即出结果）；真实系统若异步审核，`submitted` 状态会停留在待审核，
  需人工或定时任务再触发回读。
