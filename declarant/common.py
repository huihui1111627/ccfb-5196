"""Shared domain helpers: config, DB, catalog validation."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "data"
CATALOG_PATH = ROOT / "reference" / "catalog.json"

_session_ttl_env = os.environ.get("MOCK_SESSION_TTL_SECONDS")


def data_dir() -> Path:
    return Path(os.environ.get("DECLARANT_DATA_DIR", DEFAULT_DATA_DIR))


def db_path() -> Path:
    return data_dir() / "declarant.db"


def mock_db_path() -> Path:
    return data_dir() / "mock_customs.db"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def connect() -> sqlite3.Connection:
    return _connect(db_path())


def connect_mock() -> sqlite3.Connection:
    return _connect(mock_db_path())


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def session_ttl_seconds() -> int:
    """Mock customs session lifetime. Default 20 minutes (requirement)."""
    if _session_ttl_env:
        try:
            return max(5, int(_session_ttl_env))
        except ValueError:
            pass
    return 20 * 60


_lock = threading.RLock()


def init_db() -> None:
    with _lock:
        conn = connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS declarations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER,
                -- business
                contract_no TEXT,
                invoice_no TEXT NOT NULL,
                trade_mode TEXT NOT NULL DEFAULT '0110',
                nature TEXT,
                duty_mode TEXT,
                customs_code TEXT,
                port_code TEXT,
                transport_mode TEXT,
                vessel_voyage TEXT,
                trade_country TEXT,
                origin_country TEXT,
                dest_country TEXT,
                trade_terms TEXT,
                currency TEXT,
                exchange_rate REAL DEFAULT 1,
                total_amount REAL DEFAULT 0,
                freight REAL DEFAULT 0,
                insurance REAL DEFAULT 0,
                other_charges REAL DEFAULT 0,
                package_count INTEGER DEFAULT 0,
                packing_type TEXT,
                gross_weight REAL DEFAULT 0,
                net_weight REAL DEFAULT 0,
                consignor TEXT,
                consignee TEXT,
                notify_party TEXT,
                -- source + mapping
                source_file TEXT,
                source_kind TEXT,
                source_page INTEGER DEFAULT 0,
                fingerprint TEXT NOT NULL,
                field_map TEXT NOT NULL DEFAULT '{}',
                linked_json TEXT NOT NULL DEFAULT '{}',
                -- state machine: queued/validated/hold/recording/submitted/approved/rejected/abnormal
                status TEXT NOT NULL DEFAULT 'queued',
                mock_decl_id TEXT,
                error_reason TEXT,
                reject_reason TEXT,
                reject_category TEXT,
                reject_attempts INTEGER DEFAULT 0,
                approved_at TEXT,
                rejected_at TEXT,
                submitted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS ux_declaration_fingerprint
                ON declarations(fingerprint);
            CREATE INDEX IF NOT EXISTS ix_declaration_status ON declarations(status);

            CREATE TABLE IF NOT EXISTS declaration_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                declaration_id INTEGER NOT NULL,
                seq_no INTEGER NOT NULL,
                hs_code TEXT NOT NULL,
                goods_name TEXT,
                specification TEXT,
                brand TEXT,
                model TEXT,
                quantity REAL DEFAULT 0,
                unit TEXT,
                legal_quantity REAL,
                legal_unit TEXT,
                second_quantity REAL,
                second_unit TEXT,
                unit_price REAL DEFAULT 0,
                total_price REAL DEFAULT 0,
                origin_country TEXT,
                currency TEXT,
                elements TEXT NOT NULL DEFAULT '{}',
                reviewed INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(declaration_id) REFERENCES declarations(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS ix_items_decl ON declaration_items(declaration_id, seq_no);

            CREATE TABLE IF NOT EXISTS import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing',
                total_rows INTEGER DEFAULT 0,
                imported_rows INTEGER DEFAULT 0,
                skipped_rows INTEGER DEFAULT 0,
                error_rows INTEGER DEFAULT 0,
                split_sheets INTEGER DEFAULT 0,
                report TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS reconciliation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                declaration_id INTEGER NOT NULL,
                field_path TEXT NOT NULL,
                source_value TEXT,
                system_value TEXT,
                severity TEXT NOT NULL DEFAULT 'error',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                UNIQUE(declaration_id, field_path)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                label TEXT,
                profile_rel TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                started_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                renewed_count INTEGER DEFAULT 0,
                stopped_at TEXT
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                declaration_id INTEGER,
                session_id INTEGER,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                step TEXT,
                progress INTEGER DEFAULT 0,
                resume_at TEXT,
                attempt INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                note TEXT,
                mock_decl_id TEXT,
                payload TEXT NOT NULL DEFAULT '{}',
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(declaration_id, kind)
            );

            CREATE TABLE IF NOT EXISTS submit_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                declaration_id INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                revision INTEGER NOT NULL,
                phase TEXT NOT NULL,
                mock_decl_id TEXT,
                decl_no TEXT,
                outcome TEXT,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(fingerprint, revision)
            );

            CREATE TABLE IF NOT EXISTS rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                declaration_id INTEGER NOT NULL,
                mock_decl_id TEXT,
                decl_no TEXT,
                raw_reason TEXT NOT NULL,
                category TEXT,
                field_path TEXT,
                item_seq INTEGER,
                suggestion TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                resolved_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(declaration_id, raw_reason)
            );

            CREATE TABLE IF NOT EXISTS reconciliation_run (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                declaration_id INTEGER NOT NULL,
                mock_decl_id TEXT,
                total_fields INTEGER DEFAULT 0,
                mismatched INTEGER DEFAULT 0,
                result TEXT NOT NULL DEFAULT 'ok',
                snapshot TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS job_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                declaration_id INTEGER,
                session_id INTEGER,
                level TEXT NOT NULL DEFAULT 'info',
                event TEXT NOT NULL,
                message TEXT,
                screenshot TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                description TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                day TEXT PRIMARY KEY,
                imported_count INTEGER NOT NULL DEFAULT 0,
                submitted_count INTEGER NOT NULL DEFAULT 0,
                approved_count INTEGER NOT NULL DEFAULT 0,
                rejected_count INTEGER NOT NULL DEFAULT 0,
                duplicate_blocked INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                fingerprint TEXT,
                declaration_id INTEGER,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_audit_action_fp
                ON audit_log(action, fingerprint);
            """
        )
        conn.commit()
        conn.close()


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------
_catalog_cache: dict[str, Any] | None = None
_catalog_lock = threading.Lock()


def catalog() -> dict[str, Any]:
    global _catalog_cache
    with _catalog_lock:
        if _catalog_cache is None:
            _catalog_cache = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        return _catalog_cache


def hs_entry(hs_code: str) -> dict[str, Any] | None:
    code = (hs_code or "").strip()
    # tolerate spaces / missing check digit style: exact lookup then prefixes
    return catalog()["hs_codes"].get(code)


def normalize_hs_code(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def trade_mode_entry(code: str) -> dict[str, Any] | None:
    code = (code or "").strip()
    for item in catalog()["trade_modes"]:
        if item["code"] == code:
            return item
    return None


# Trade-mode linked fields. These are only rendered by the mock system
# after the mode is chosen (the agent must wait for them to appear).
TRADE_MODE_FIELDS: dict[str, list[dict[str, Any]]] = {
    "0110": [
        {"name": "trade_terms", "label": "成交方式", "kind": "select", "options_from": "trade_terms"},
        {"name": "freight", "label": "运费", "kind": "number"},
        {"name": "insurance", "label": "保费", "kind": "number"},
        {"name": "other_charges", "label": "杂费", "kind": "number"},
    ],
    "0615": [
        {"name": "trade_terms", "label": "成交方式", "kind": "select", "options_from": "trade_terms"},
        {"name": "processing_ratio", "label": "加工贸易比例(%)", "kind": "number"},
        {"name": "bonded_register_no", "label": "加工贸易手册号", "kind": "text"},
    ],
    "0214": [
        {"name": "trade_terms", "label": "成交方式", "kind": "select", "options_from": "trade_terms"},
        {"name": "processing_ratio", "label": "加工贸易比例(%)", "kind": "number"},
        {"name": "bonded_register_no", "label": "加工贸易手册号", "kind": "text"},
    ],
    "1200": [
        {"name": "warehouse_no", "label": "保税仓库号", "kind": "text"},
        {"name": "trade_terms", "label": "成交方式", "kind": "select", "options_from": "trade_terms"},
        {"name": "storage_days", "label": "仓储期限(天)", "kind": "number"},
    ],
    "1210": [
        {"name": "ecommerce_order_no", "label": "电商订单号", "kind": "text"},
        {"name": "ecommerce_platform", "label": "电商平台", "kind": "select",
         "options": ["天猫国际", "京东国际", "拼多多国际", "抖音国际", "其他"]},
        {"name": "trade_terms", "label": "成交方式", "kind": "select", "options_from": "trade_terms"},
    ],
    "2600": [
        {"name": "guarantee_no", "label": "担保编号", "kind": "text"},
        {"name": "guarantee_amount", "label": "担保金额", "kind": "number"},
        {"name": "trade_terms", "label": "成交方式", "kind": "select", "options_from": "trade_terms"},
    ],
    "4561": [
        {"name": "original_decl_no", "label": "原报关单号", "kind": "text"},
        {"name": "return_reason", "label": "退运原因", "kind": "select",
         "options": ["品质不符", "规格不符", "订单取消", "错发货物", "其他"]},
        {"name": "trade_terms", "label": "成交方式", "kind": "select", "options_from": "trade_terms"},
    ],
    "1523": [
        {"name": "lease_term_months", "label": "租赁期限(月)", "kind": "number"},
        {"name": "trade_terms", "label": "成交方式", "kind": "select", "options_from": "trade_terms"},
    ],
}


def trade_mode_fields(code: str) -> list[dict[str, Any]]:
    return TRADE_MODE_FIELDS.get((code or "").strip(), [])


def trade_mode_derived(code: str) -> dict[str, str]:
    entry = trade_mode_entry(code) or {}
    return {"nature": entry.get("nature", ""), "duty_mode": entry.get("duty_mode", "")}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
@dataclass
class Issue:
    severity: str  # error | warning
    field: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "field": self.field, "message": self.message}


def validate_item(item: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    raw_hs = item.get("hs_code") or ""
    hs_code = normalize_hs_code(raw_hs)
    if len(hs_code) != 10:
        issues.append(Issue("error", f"{item.get('seq_no','?')}.hs_code",
                            f"HS编码须为10位数字，当前为“{raw_hs}”"))
    entry = hs_entry(hs_code)
    if not hs_code or len(hs_code) != 10:
        return issues
    if not entry:
        issues.append(Issue("error", f"{item.get('seq_no','?')}.hs_code",
                            f"HS编码 {hs_code} 不在本地税则库中"))
        return issues

    elements = item.get("elements") or {}
    if isinstance(elements, str):
        try:
            elements = json.loads(elements or "{}")
        except json.JSONDecodeError:
            elements = {}
    for spec in entry["elements"]:
        val = str(elements.get(spec["key"], "") or "").strip()
        if spec.get("required") and not val:
            issues.append(Issue_wrap(item, spec, f"申报要素[{spec['label']}]缺失"))
        if spec["kind"] == "select" and val and val not in spec["options"]:
            issues.append(Issue_wrap(item, spec,
                                      f"申报要素[{spec['label']}]取值“{val}”不在允许范围"))
        if spec["kind"] == "number" and val:
            try:
                float(val)
            except ValueError:
                issues.append(Issue_wrap(item, spec, f"申报要素[{spec['label']}]须为数字"))

    qty = _as_float(item.get("quantity"))
    if qty <= 0:
        issues.append(Issue("error", f"{item.get('seq_no','?')}.quantity", "数量必须大于0"))
    unit = (item.get("unit") or "").strip()
    allowed_units = catalog()["units"]
    if not unit:
        issues.append(Issue("error", f"{item.get('seq_no','?')}.unit", "成交单位不能为空"))
    elif unit not in allowed_units:
        issues.append(Issue("warning", f"{item.get('seq_no','?')}.unit",
                             f"成交单位“{unit}”不在常用单位表，系统可能拒收"))
    legal_unit = entry.get("legal_unit")
    legal_qty = item.get("legal_quantity")
    if legal_qty is None or _as_float(legal_qty) <= 0:
        issues.append(Issue("error", f"{item.get('seq_no','?')}.legal_quantity",
                             f"法定数量不能为空（法定单位 {legal_unit}）"))
    if legal_unit and (item.get("legal_unit") or "") not in ("", legal_unit):
        issues.append(Issue("warning", f"{item.get('seq_no','?')}.legal_unit",
                             f"法定单位应为 {legal_unit}"))
    if entry.get("second_unit"):
        sq = item.get("second_quantity")
        if sq is None or _as_float(sq) <= 0:
            issues.append(Issue("warning", f"{item.get('seq_no','?')}.second_quantity",
                                 f"{entry['second_unit']}为法定第二单位，建议填写第二数量"))
    unit_price = _as_float(item.get("unit_price"))
    total_price = _as_float(item.get("total_price"))
    if total_price <= 0 and unit_price <= 0:
        issues.append(Issue("error", f"{item.get('seq_no','?')}.total_price", "总价不能为空"))
    if qty > 0 and unit_price > 0 and total_price > 0:
        if abs(qty * unit_price - total_price) > max(0.02, total_price * 0.005):
            issues.append(Issue("warning", f"{item.get('seq_no','?')}.total_price",
                                 f"数量×单价={qty*unit_price:.2f}，与总价{total_price:.2f}不一致"))
    origin = (item.get("origin_country") or "").strip()
    if not origin:
        issues.append(Issue("error", f"{item.get('seq_no','?')}.origin_country", "原产国不能为空"))
    elif origin not in catalog()["countries"]:
        issues.append(Issue("error", f"{item.get('seq_no','?')}.origin_country",
                             f"原产国代码 {origin} 不在国别代码表"))
    return issues


def Issue_wrap(item: dict[str, Any], spec: dict[str, Any], message: str) -> Issue:
    return Issue("error", f"{item.get('seq_no','?')}.elements.{spec['key']}", message)


def validate_declaration(decl: dict[str, Any], items: list[dict[str, Any]]) -> list[Issue]:
    issues: list[Issue] = []
    mode = (decl.get("trade_mode") or "").strip()
    if not trade_mode_entry(mode):
        issues.append(Issue("error", "trade_mode", f"贸易方式 {mode or '(空)'} 不在代码表"))
    for fld in ("port_code", "transport_mode", "trade_country", "currency", "packing_type"):
        val = (decl.get(fld) or "").strip()
        table = {
            "port_code": catalog()["ports"], "transport_mode": catalog()["transport_modes"],
            "trade_country": catalog()["countries"], "currency": catalog()["currencies"],
            "packing_type": catalog()["packing_types"],
        }[fld]
        if not val:
            issues.append(Issue("error", fld, f"{fld} 不能为空"))
        elif val not in table:
            issues.append(Issue("error", fld, f"{fld} 取值“{val}”不在代码表"))
    if not (decl.get("invoice_no") or "").strip():
        issues.append(Issue("error", "invoice_no", "发票号不能为空"))
    if not items:
        issues.append(Issue("error", "items", "至少需要一条商品明细"))
    # trade-mode linked requirements (the actual system ask for these)
    for fld in trade_mode_fields(mode):
        key = fld["name"]
        if fld["name"] in ("trade_terms", "freight", "insurance", "other_charges"):
            continue  # freight etc. may legitimately be empty/0
        if fld["kind"] in ("text", "select") and not str(decl.get(key, "") or "").strip():
            issues.append(Issue("error", key, f"贸易方式 {mode} 必填联动字段[{fld['label']}]为空"))
    for it in items:
        issues.extend(validate_item(it))
    # totals
    sum_total = round(sum(_as_float(it.get("total_price")) for it in items), 2)
    if decl.get("total_amount") and abs(float(decl["total_amount"]) - sum_total) > 0.05 and sum_total > 0:
        issues.append(Issue("warning", "total_amount",
                             f"表头总价 {decl['total_amount']} 与明细合计 {sum_total} 不一致"))
    return issues


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def severity_counts(issues: list[Issue]) -> dict[str, int]:
    return {
        "error": sum(1 for i in issues if i.severity == "error"),
        "warning": sum(1 for i in issues if i.severity == "warning"),
    }


def has_blocking(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)


def add_to_daily(conn: sqlite3.Connection, column: str, delta: int = 1, day: str | None = None) -> None:
    day = day or today_str()
    conn.execute(
        "INSERT INTO daily_stats(day, updated_at) VALUES(?,?) ON CONFLICT(day) DO NOTHING",
        (day, now_iso()),
    )
    conn.execute(
        f"UPDATE daily_stats SET {column}={column}+?, updated_at=? WHERE day=?",
        (delta, now_iso(), day),
    )


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value: Any, description: str = "") -> None:
    conn.execute(
        "INSERT INTO settings(key, value, description, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), description, now_iso()),
    )


def seed_settings(conn: sqlite3.Connection) -> None:
    defaults = {
        "mock_base_url": ("http://127.0.0.1:8099", "Mock 申报系统根地址"),
        "mock_username": ("demo", "Mock 申报系统账号"),
        "mock_password": ("demo123", "Mock 申报系统密码"),
        "worker_concurrency": (1, "同一时刻允许的浏览器会话数"),
        "reject_simulation_rate": ("mock", "退单模拟开关：mock 表示使用 Mock 系统设定"),
        "session_renew_seconds": (60, "定时检查会话是否过期"),
    }
    for key, (value, desc) in defaults.items():
        conn.execute(
            "INSERT INTO settings(key, value, description, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO NOTHING",
            (key, json.dumps(value, ensure_ascii=False), desc, now_iso()),
        )


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def ensure_dirs() -> None:
    d = data_dir()
    for sub in ("screenshots", "uploads", "exports", "logs", "profiles"):
        (d / sub).mkdir(parents=True, exist_ok=True)
