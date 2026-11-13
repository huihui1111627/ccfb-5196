"""Mock 海关申报系统 — local simulation used to exercise the RPA worker.

Deliberately close to the real workflow:
  * login with a server-side session that expires after N seconds of inactivity
  * trade-mode linked fields are only rendered after the mode is chosen (JS fetch)
  * goods lines are entered page by page (3 lines per page)
  * submission gets an 18-digit customs declaration number, then random 退单
  * client_ref is a unique idempotency key: creating / submitting the same
    client_ref twice can never produce two declarations
"""
from __future__ import annotations

import json
import os
import random
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

from declarant.common import (catalog, connect_mock, ensure_dirs, hs_entry,
                              normalize_hs_code, now_iso, session_ttl_seconds,
                              trade_mode_fields)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("MOCK_SECRET", "mock-customs-local-dev-secret")
app.config["JSON_AS_ASCII"] = False

SESSION_TTL = session_ttl_seconds()
IDLE_RENEW = os.environ.get("MOCK_SESSION_ABSOLUTE", "0") != "1"
REJECT_RATE = float(os.environ.get("MOCK_REJECT_RATE", "0.2"))
REJECT_RATE_REVISION = float(os.environ.get("MOCK_REJECT_RATE_REVISION", "0.02"))
PAGE_SIZE = 3

_db_lock = threading.RLock()
_schema_ready = False


def init_mock_db() -> None:
    global _schema_ready
    with _db_lock:
        if _schema_ready:
            return
        ensure_dirs()
        conn = connect_mock()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                username TEXT PRIMARY KEY,
                password TEXT NOT NULL,
                display_name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions(
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                ip TEXT
            );
            CREATE TABLE IF NOT EXISTS declarations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decl_no TEXT UNIQUE,
                client_ref TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'draft',
                invoice_no TEXT,
                trade_mode TEXT,
                header_json TEXT NOT NULL DEFAULT '{}',
                item_count INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0,
                reject_reason TEXT,
                reject_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                submitted_at TEXT,
                reviewed_at TEXT,
                created_by TEXT
            );
            CREATE TABLE IF NOT EXISTS declaration_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                declaration_id INTEGER NOT NULL,
                seq_no INTEGER NOT NULL,
                data_json TEXT NOT NULL,
                saved_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(declaration_id, seq_no)
            );
            CREATE TABLE IF NOT EXISTS review_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                declaration_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                outcome TEXT,
                reason TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submit_attempts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                declaration_id INTEGER NOT NULL,
                client_ref TEXT NOT NULL,
                revision INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_decl_client_ref
                ON declarations(client_ref);
            """
        )
        for username, password, display in (("demo", "demo123", "报关员-演示"),
                                            ("admin", "admin123", "系统管理员")):
            conn.execute(
                "INSERT INTO users(username,password,display_name) VALUES(?,?,?) "
                "ON CONFLICT(username) DO NOTHING", (username, password, display))
        conn.commit()
        conn.close()
        _schema_ready = True


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def db() -> sqlite3.Connection:
    if not _schema_ready:
        init_mock_db()
    if "mock_db" not in g:
        g.mock_db = connect_mock()
    return g.mock_db


@app.teardown_appcontext
def _close_db(_exc):
    conn = g.pop("mock_db", None)
    if conn is not None:
        conn.close()


def current_session(conn: sqlite3.Connection):
    token = request.cookies.get("mq_session")
    if not token:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if not row:
        return None
    if row["expires_at"] <= now_iso():
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return dict(row, expired=True)
    return dict(row)


def protected(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        conn = db()
        sess = current_session(conn)
        if sess is None or sess.get("expired"):
            next_url = request.full_path if request.method == "GET" else url_for("index")
            expired = "expired=1" if sess and sess.get("expired") else ""
            return redirect(url_for("login", next=next_url, **({"expired": "1"} if expired else {})))
        if IDLE_RENEW:
            conn.execute("UPDATE sessions SET last_seen_at=?, expires_at=? WHERE token=?",
                         (now_iso(),
                          (datetime.now() + timedelta(seconds=SESSION_TTL)).strftime("%Y-%m-%d %H:%M:%S"),
                          sess["token"]))
            conn.commit()
            sess["expires_at"] = (datetime.now() + timedelta(seconds=SESSION_TTL)).strftime("%Y-%m-%d %H:%M:%S")
        g.sess = sess
        g.user = sess["username"]
        return view(*args, **kwargs)
    return wrapper


def expires_epoch(sess) -> int:
    return int(datetime.strptime(sess["expires_at"], "%Y-%m-%d %H:%M:%S").timestamp() * 1000)


@app.context_processor
def inject_globals():
    sess = g.get("sess")
    return {
        "catalog": catalog(),
        "session_expires_at": expires_epoch(sess) if sess else 0,
        "session_ttl": SESSION_TTL,
        "current_user": g.get("user"),
        "now": datetime.now(),
    }


def save_screenshot_friendly(_msg):  # placeholder hook, no-op on mock side
    pass


def get_declaration(conn, decl_id):
    row = conn.execute("SELECT * FROM declarations WHERE id=?", (decl_id,)).fetchone()
    if not row:
        abort(404)
    return row


def items_for(conn, decl_id) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT * FROM declaration_items WHERE declaration_id=? ORDER BY seq_no", (decl_id,)
    ).fetchall()
    out = {}
    for r in rows:
        out[r["seq_no"]] = json.loads(r["data_json"] or "{}")
    return out


def assign_decl_no(conn, decl_id: int) -> str:
    stamp = datetime.now().strftime("%Y%m%d")
    return f"2231{stamp}{decl_id:06d}"


def validate_submission(conn, decl, items):
    errors = []
    h = json.loads(decl["header_json"] or "{}")
    for key, label in (("invoice_no", "发票号"), ("trade_mode", "贸易方式"),
                       ("port_code", "申报地海关"), ("currency", "币制")):
        if not str(h.get(key, "") or "").strip():
            errors.append(f"表头[{label}]不能为空")
    mode_fields = trade_mode_fields(decl["trade_mode"])
    for f in mode_fields:
        if f["name"] in ("trade_terms", "freight", "insurance", "other_charges"):
            continue
        if not str(h.get(f["name"], "") or "").strip():
            errors.append(f"贸易方式[{decl['trade_mode']}]联动字段[{f['label']}]不能为空")
    expected = decl["item_count"]
    missing = [seq for seq in range(1, expected + 1) if seq not in items]
    if missing:
        errors.append(f"商品明细第 {','.join(map(str, missing))} 项未录入")
    for seq, it in items.items():
        if not normalize_hs_code(it.get("hs_code", "")):
            errors.append(f"第{seq}项 HS 编码不能为空")
        if not str(it.get("goods_name", "") or "").strip():
            errors.append(f"第{seq}项 商品名称不能为空")
        try:
            if float(it.get("quantity") or 0) <= 0:
                errors.append(f"第{seq}项 成交数量必须大于 0")
            if float(it.get("total_price") or 0) <= 0:
                errors.append(f"第{seq}项 总价必须大于 0")
        except (TypeError, ValueError):
            errors.append(f"第{seq}项 数量/总价格式错误")
    return errors, h


def make_reject_reason(conn, decl, items, h) -> tuple[str, str]:
    """Return (code, human readable reason). Reasons point at a real field."""
    candidates = ["element", "price", "header", "legal", "doc"]
    conn_rng = random
    code = conn_rng.choice(candidates)
    seq = sorted(items)[0] if items else 1
    item = items.get(seq, {})
    if code == "element":
        entry = hs_entry(normalize_hs_code(item.get("hs_code", ""))) or {}
        specs = entry.get("elements") or [{"key": "brand_type", "label": "品牌类型"}]
        spec = conn_rng.choice(specs)
        return "ELEMENT", (f"[要素不符] 第{seq}项商品申报要素[{spec['label']}]"
                           f"填报与税则要求不一致，请核实后重新申报。")
    if code == "price":
        unit_price = float(item.get("unit_price") or 0)
        qty = float(item.get("quantity") or 0)
        total = float(item.get("total_price") or 0)
        diff = round(qty * unit_price - total, 2)
        return "PRICE", (f"[价格不符] 第{seq}项商品总价与数量×单价不一致"
                         f"（差额 {diff:.2f}），请更正成交价格。")
    if code == "legal":
        return "QUANTITY", (f"[数量不符] 第{seq}项法定数量与成交数量折算关系不符，"
                            f"请按税则法定单位重新折算。")
    if code == "doc":
        return "DOCUMENT", ("[单证不符] 随附单证中的发票号与表头发票号不一致，请核对后重新申报。")
    mode_fields = [f for f in trade_mode_fields(decl["trade_mode"])
                   if f["name"] not in ("trade_terms", "freight", "insurance", "other_charges")]
    if mode_fields:
        f = mode_fields[0]
        return "HEADER", (f"[表头不符] 贸易方式[{decl['trade_mode']}]下"
                          f"[{f['label']}]未按规定填报，请补正。")
    return "HEADER", "[表头不符] 表头申报信息与随附单证不一致，请补正后重新申报。"


HEADER_LABELS = {
    "invoice_no": "发票号", "contract_no": "合同协议号", "port_code": "申报地海关",
    "transport_mode": "运输方式", "vessel_voyage": "运输工具/航次",
    "trade_country": "贸易国(地区)", "dest_country": "运抵国(地区)", "currency": "币制",
    "exchange_rate": "结汇汇率", "package_count": "包装件数", "packing_type": "包装种类",
    "gross_weight": "毛重(千克)", "net_weight": "净重(千克)", "consignee": "境内收发货人",
    "consignor": "境外收发货人", "trade_terms": "成交方式", "freight": "运费",
    "insurance": "保费", "other_charges": "杂费", "processing_ratio": "加工贸易比例(%)",
    "bonded_register_no": "加工贸易手册号", "warehouse_no": "保税仓库号",
    "storage_days": "仓储期限(天)", "ecommerce_order_no": "电商订单号",
    "ecommerce_platform": "电商平台", "guarantee_no": "担保编号",
    "guarantee_amount": "担保金额", "original_decl_no": "原报关单号",
    "return_reason": "退运原因", "lease_term_months": "租赁期限(月)",
    "item_count": "商品项数", "nature": "监管方式", "duty_mode": "征免性质",
}


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "mock-customs", "time": now_iso()})


@app.route("/login", methods=["GET", "POST"])
def login():
    init_mock_db()
    conn = db()
    error = None
    expired = request.args.get("expired") == "1"
    nxt = request.args.get("next") or request.form.get("next") or "/"
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not user or user["password"] != password:
            error = "用户名或密码错误"
        else:
            token = secrets.token_hex(24)
            ts = datetime.now()
            conn.execute(
                "INSERT INTO sessions(token,username,created_at,last_seen_at,expires_at,ip) "
                "VALUES(?,?,?,?,?,?)",
                (token, username, ts.strftime("%Y-%m-%d %H:%M:%S"),
                 ts.strftime("%Y-%m-%d %H:%M:%S"),
                 (ts + timedelta(seconds=SESSION_TTL)).strftime("%Y-%m-%d %H:%M:%S"),
                 request.remote_addr))
            conn.commit()
            resp = redirect(nxt if nxt.startswith("/") else "/", code=303)
            resp.set_cookie("mq_session", token, max_age=SESSION_TTL, httponly=True, samesite="Lax")
            return resp
    return render_template("login.html", error=error, expired=expired, next=nxt,
                           ttl_minutes=round(SESSION_TTL / 60, 1))


@app.route("/logout")
def logout():
    conn = db()
    token = request.cookies.get("mq_session")
    if token:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
    resp = redirect(url_for("login"))
    resp.delete_cookie("mq_session")
    return resp


@app.route("/api/session/peek")
def session_peek():
    conn = db()
    sess = current_session(conn)
    if not sess or sess.get("expired"):
        return jsonify({"authenticated": False, "remaining": 0}), 200
    return jsonify({"authenticated": True, "username": sess["username"],
                    "remaining": max(0, expires_epoch(sess) // 1000 - int(time.time())),
                    "expires_at": sess["expires_at"]})


@app.route("/api/test/expire-all", methods=["POST", "GET"])
def expire_all():
    if os.environ.get("MOCK_ALLOW_TEST_ENDPOINTS", "1") != "1":
        abort(404)
    conn = db()
    conn.execute("UPDATE sessions SET expires_at=?",
                 ((datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),))
    conn.commit()
    return jsonify({"ok": True, "message": "所有会话已强制过期"})


@app.route("/api/test/reject-rate", methods=["POST", "GET"])
def set_reject_rate():
    """Test/demo hook: adjust the simulated rejection rate at runtime."""
    if os.environ.get("MOCK_ALLOW_TEST_ENDPOINTS", "1") != "1":
        abort(404)
    global REJECT_RATE, REJECT_RATE_REVISION
    rate = request.args.get("rate", type=float)
    revision = request.args.get("revision", type=float)
    if rate is not None:
        REJECT_RATE = max(0.0, min(1.0, rate))
    if revision is not None:
        REJECT_RATE_REVISION = max(0.0, min(1.0, revision))
    return jsonify({"ok": True, "reject_rate": REJECT_RATE,
                    "reject_rate_revision": REJECT_RATE_REVISION})


@app.route("/api/test/reset", methods=["POST"])
def reset_all():
    if os.environ.get("MOCK_ALLOW_TEST_ENDPOINTS", "1") != "1":
        abort(404)
    conn = db()
    conn.executescript(
        "DELETE FROM declaration_items; DELETE FROM declarations;"
        "DELETE FROM review_history; DELETE FROM submit_attempts; DELETE FROM sessions;")
    conn.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# declarations
# --------------------------------------------------------------------------
@app.route("/")
@protected
def index():
    conn = db()
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    sql = "SELECT * FROM declarations WHERE 1=1"
    args = []
    if q:
        sql += (" AND (invoice_no LIKE ? OR client_ref LIKE ? OR decl_no LIKE ?"
                " OR CAST(id AS TEXT)=?)")
        args += [f"%{q}%", f"%{q}%", f"%{q}%", q]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT 200"
    rows = conn.execute(sql, args).fetchall()
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM declarations GROUP BY status").fetchall()}
    return render_template("index.html", rows=rows, q=q, status=status, counts=counts,
                           status_labels={"draft": "草稿", "submitted": "已申报",
                                          "approved": "审核通过", "rejected": "已退单"})


@app.route("/declarations/new", methods=["GET", "POST"])
@protected
def new_declaration():
    conn = db()
    notice = None
    error = None
    if request.method == "POST":
        client_ref = request.form.get("client_ref", "").strip()
        invoice_no = request.form.get("invoice_no", "").strip()
        trade_mode = request.form.get("trade_mode", "").strip()
        try:
            item_count = int(request.form.get("item_count") or 0)
        except ValueError:
            item_count = 0
        if not client_ref or not invoice_no or not trade_mode:
            error = "客户参考号、发票号、贸易方式为必填项"
        elif item_count <= 0:
            error = "商品项数必须大于 0"
        else:
            existing = conn.execute("SELECT * FROM declarations WHERE client_ref=?",
                                    (client_ref,)).fetchone()
            if existing:
                if existing["status"] in ("submitted", "approved"):
                    conn.execute(
                        "INSERT INTO review_history(declaration_id,action,outcome,reason,created_at)"
                        " VALUES(?,?,?,?,?)",
                        (existing["id"], "duplicate_block", "BLOCKED",
                         f"重复申报拦截：客户参考号 {client_ref} 已存在", now_iso()))
                    conn.commit()
                    error = (f"重复申报：客户参考号 {client_ref} 已存在（报关单号 "
                             f"{existing['decl_no'] or '未编号'}，状态 "
                             f"{ {'draft':'草稿','submitted':'已申报','approved':'审核通过','rejected':'已退单'}.get(existing['status']) }），"
                             f"不能重复建单。")
                else:
                    notice = f"该客户参考号已有草稿/退单记录，已复用原单（ID {existing['id']}）。"
                    return redirect(url_for("declaration_detail", decl_id=existing["id"]))
            else:
                ts = now_iso()
                cur = conn.execute(
                    "INSERT INTO declarations(client_ref,invoice_no,trade_mode,header_json,"
                    "item_count,status,created_at,updated_at,created_by) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (client_ref, invoice_no, trade_mode,
                     json.dumps({"invoice_no": invoice_no, "trade_mode": trade_mode}, ensure_ascii=False),
                     item_count, "draft", ts, ts, g.user))
                decl_id = cur.lastrowid
                conn.execute("INSERT INTO review_history(declaration_id,action,outcome,detail,created_at)"
                             " VALUES(?,?,?,?,?)", (decl_id, "create", "OK", client_ref, ts))
                conn.commit()
                return redirect(url_for("header_edit", decl_id=decl_id))
    return render_template("new.html", notice=notice, error=error,
                           preset_client_ref=request.args.get("ref", ""),
                           preset_invoice=request.args.get("invoice", ""))


@app.route("/declarations/<int:decl_id>")
@protected
def declaration_detail(decl_id):
    conn = db()
    decl = get_declaration(conn, decl_id)
    items = items_for(conn, decl_id)
    header = json.loads(decl["header_json"] or "{}")
    history = conn.execute(
        "SELECT * FROM review_history WHERE declaration_id=? ORDER BY id DESC", (decl_id,)
    ).fetchall()
    return render_template("detail.html", decl=decl, items=items, header=header,
                           header_labels=HEADER_LABELS,
                           history=history, mode_fields=trade_mode_fields(decl["trade_mode"]),
                           status_label={"draft": "草稿", "submitted": "已申报",
                                         "approved": "审核通过", "rejected": "已退单"}.get(decl["status"], decl["status"]))


@app.route("/declarations/<int:decl_id>/header", methods=["GET", "POST"])
@protected
def header_edit(decl_id):
    conn = db()
    decl = get_declaration(conn, decl_id)
    header = json.loads(decl["header_json"] or "{}")
    error = None
    if request.method == "POST":
        if decl["status"] in ("submitted", "approved"):
            error = "该报关单已申报/审核通过，不能修改表头"
        else:
            form = request.form.to_dict()
            mode = form.get("trade_mode", "").strip()
            header = {k: (v.strip() if isinstance(v, str) else v) for k, v in form.items()}
            header["trade_mode"] = mode
            header["item_count"] = decl["item_count"]
            entry = next((m for m in catalog()["trade_modes"] if m["code"] == mode), None)
            if entry:
                header["nature"] = entry["nature"]
                header["duty_mode"] = entry["duty_mode"]
            for nf in ("freight", "insurance", "other_charges"):
                if header.get(nf) in ("", None):
                    header[nf] = "0"
            conn.execute("UPDATE declarations SET header_json=?, trade_mode=?, invoice_no=?, "
                         "status=?, updated_at=? WHERE id=?",
                         (json.dumps(header, ensure_ascii=False), mode,
                          header.get("invoice_no", decl["invoice_no"]),
                          "draft" if decl["status"] == "rejected" else decl["status"],
                          now_iso(), decl_id))
            conn.commit()
            flash("表头已保存", "ok")
            return redirect(url_for("declaration_detail", decl_id=decl_id))
    return render_template("header.html", decl=decl, header=header, error=error,
                           mode_fields=trade_mode_fields(header.get("trade_mode") or decl["trade_mode"]),
                           trade_modes=catalog()["trade_modes"],
                           ports=catalog()["ports"], transport_modes=catalog()["transport_modes"],
                           countries=catalog()["countries"], currencies=catalog()["currencies"],
                           packing_types=catalog()["packing_types"])


@app.route("/api/linked-fields")
@protected
def api_linked_fields():
    mode = request.args.get("mode", "")
    fields = []
    for f in trade_mode_fields(mode):
        item = dict(f)
        if f.get("options_from"):
            item["options"] = list(catalog()[f["options_from"]].keys())
            item["option_labels"] = [catalog()[f["options_from"]][k] for k in item["options"]]
        fields.append(item)
    entry = next((m for m in catalog()["trade_modes"] if m["code"] == mode), None)
    return jsonify({"trade_mode": mode, "label": entry["label"] if entry else "",
                    "nature": entry["nature"] if entry else "",
                    "duty_mode": entry["duty_mode"] if entry else "",
                    "fields": fields})


@app.route("/api/elements")
@protected
def api_elements():
    hs = normalize_hs_code(request.args.get("hs", ""))
    entry = hs_entry(hs)
    if not entry:
        return jsonify({"ok": False, "error": "未找到该商品编码"}), 404
    return jsonify({"ok": True, "hs_code": hs, "name": entry["name"],
                    "legal_unit": entry.get("legal_unit"),
                    "second_unit": entry.get("second_unit"),
                    "elements": entry["elements"]})


@app.route("/declarations/<int:decl_id>/items")
@protected
def items_page(decl_id):
    conn = db()
    decl = get_declaration(conn, decl_id)
    items = items_for(conn, decl_id)
    total = max(1, decl["item_count"])
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    page = min(max(1, page), pages)
    start = (page - 1) * PAGE_SIZE + 1
    end = min(total, page * PAGE_SIZE)
    slots = list(range(start, end + 1))
    mode = decl["trade_mode"]
    bonded = mode in ("0615", "0214")
    element_labels: dict[str, str] = {}
    for it in items.values():
        entry = hs_entry(normalize_hs_code(it.get("hs_code", "")))
        for spec in (entry or {}).get("elements", []):
            element_labels[spec["key"]] = spec["label"]
    return render_template("items.html", decl=decl, items=items, slots=slots,
                           page=page, pages=pages, total=total, mode=mode,
                           bonded=bonded, packing=catalog()["units"],
                           element_labels=element_labels)


@app.route("/declarations/<int:decl_id>/items/<int:seq_no>", methods=["POST"])
@protected
def item_save(decl_id, seq_no):
    conn = db()
    decl = get_declaration(conn, decl_id)
    if decl["status"] in ("submitted", "approved"):
        flash("该报关单已申报/审核通过，不能修改明细", "err")
        return redirect(url_for("items_page", decl_id=decl_id))
    if seq_no < 1 or seq_no > decl["item_count"]:
        flash(f"项号 {seq_no} 超出商品项数范围", "err")
        return redirect(url_for("items_page", decl_id=decl_id))
    data = {}
    for key in ("hs_code", "goods_name", "specification", "brand", "model", "quantity",
                "unit", "legal_quantity", "legal_unit", "second_quantity", "second_unit",
                "unit_price", "total_price", "origin_country", "bonded_item_no"):
        data[key] = request.form.get(key, "").strip()
    data["elements"] = {}
    for k, v in request.form.items():
        if k.startswith("element_"):
            data["elements"][k[len("element_"):]] = v.strip()
    hs = normalize_hs_code(data["hs_code"])
    entry = hs_entry(hs)
    if entry and not data["legal_unit"]:
        data["legal_unit"] = entry.get("legal_unit") or ""
    if entry and entry.get("second_unit") and not data["second_unit"]:
        data["second_unit"] = entry["second_unit"]
    ts = now_iso()
    exists = conn.execute("SELECT id FROM declaration_items WHERE declaration_id=? AND seq_no=?",
                          (decl_id, seq_no)).fetchone()
    vals = (json.dumps(data, ensure_ascii=False),)
    if exists:
        conn.execute("UPDATE declaration_items SET data_json=?, updated_at=? WHERE id=?",
                     (vals[0], ts, exists["id"]))
    else:
        conn.execute("INSERT INTO declaration_items(declaration_id,seq_no,data_json,saved_at,updated_at)"
                     " VALUES(?,?,?,?,?)", (decl_id, seq_no, vals[0], ts, ts))
    conn.execute("UPDATE declarations SET updated_at=? WHERE id=?", (ts, decl_id))
    conn.commit()
    flash(f"第 {seq_no} 项已保存", "ok")
    # keep the user on the page; the operator explicitly navigates to next page
    return redirect(url_for("items_page", decl_id=decl_id, page=request.form.get("page", 1)))


@app.route("/declarations/<int:decl_id>/submit", methods=["GET", "POST"])
@protected
def submit_page(decl_id):
    conn = db()
    decl = get_declaration(conn, decl_id)
    items = items_for(conn, decl_id)
    header = json.loads(decl["header_json"] or "{}")
    errors, _ = validate_submission(conn, decl, items)
    result = None
    if request.method == "POST":
        # idempotency: an already approved declaration can never be submitted again
        if decl["status"] == "approved":
            conn.execute("INSERT INTO review_history(declaration_id,action,outcome,reason,created_at)"
                         " VALUES(?,?,?,?,?)",
                         (decl_id, "duplicate_block", "BLOCKED",
                          "已审核通过的报关单再次提交被拦截", now_iso()))
            conn.commit()
            return render_template("submit.html", decl=decl, items=items, header=header,
                                   errors=errors, result={"ok": False, "duplicate": True,
                                                          "message": f"该报关单已审核通过（{decl['decl_no']}），不能重复申报。"})
        if decl["status"] == "submitted":
            return render_template("submit.html", decl=decl, items=items, header=header,
                                   errors=errors, result={"ok": False, "duplicate": True,
                                                          "message": "该报关单已提交，正在审核，不能重复申报。"})
        if errors:
            return render_template("submit.html", decl=decl, items=items, header=header,
                                   errors=errors, result={"ok": False, "message": "校验不通过，未提交"})
        ts = now_iso()
        rev = decl["revision"]
        # first submission assigns the number, resubmission keeps it
        decl_no = decl["decl_no"] or assign_decl_no(conn, decl_id)
        rate = REJECT_RATE if rev == 0 else REJECT_RATE_REVISION
        conn.execute("UPDATE declarations SET status='submitted', decl_no=?, submitted_at=?, "
                     "updated_at=?, reject_reason=NULL, reject_code=NULL WHERE id=?",
                     (decl_no, ts, ts, decl_id))
        conn.execute("INSERT INTO submit_attempts(declaration_id,client_ref,revision,outcome,note,created_at)"
                     " VALUES(?,?,?,?,?,?)",
                     (decl_id, decl["client_ref"], rev, "SUBMITTED", "系统受理", ts))
        conn.commit()

        # synchronous review (local mock)
        if random.random() < rate:
            code, reason = make_reject_reason(conn, decl, items, header)
            conn.execute("UPDATE declarations SET status='rejected', reject_reason=?, reject_code=?, "
                         "reviewed_at=?, updated_at=?, revision=revision+1 WHERE id=?",
                         (reason, code, ts, ts, decl_id))
            conn.execute("INSERT INTO submit_attempts(declaration_id,client_ref,revision,outcome,note,created_at)"
                         " VALUES(?,?,?,?,?,?)",
                         (decl_id, decl["client_ref"], rev, "REJECTED", reason, ts))
            conn.execute("INSERT INTO review_history(declaration_id,action,outcome,reason,created_at)"
                         " VALUES(?,?,?,?,?)", (decl_id, "review", "REJECTED", reason, ts))
            conn.commit()
            result = {"ok": False, "rejected": True, "decl_no": decl_no,
                      "reason": reason, "code": code,
                      "message": "审核未通过（退单）"}
        else:
            conn.execute("UPDATE declarations SET status='approved', reviewed_at=?, updated_at=?, "
                         "revision=revision+1 WHERE id=?",
                         (ts, ts, decl_id))
            conn.execute("INSERT INTO submit_attempts(declaration_id,client_ref,revision,outcome,note,created_at)"
                         " VALUES(?,?,?,?,?,?)",
                         (decl_id, decl["client_ref"], rev, "APPROVED", "审核通过", ts))
            conn.execute("INSERT INTO review_history(declaration_id,action,outcome,reason,created_at)"
                         " VALUES(?,?,?,?,?)", (decl_id, "review", "APPROVED", "审核通过", ts))
            conn.commit()
            result = {"ok": True, "approved": True, "decl_no": decl_no,
                      "message": "审核通过，已结关"}
        return render_template("submit.html", decl=get_declaration(conn, decl_id),
                               items=items_for(conn, decl_id), header=header,
                               errors=[], result=result)
    return render_template("submit.html", decl=decl, items=items, header=header,
                           errors=errors, result=None)


@app.route("/lookup")
@protected
def lookup():
    conn = db()
    ref = request.args.get("ref", "").strip()
    rows = []
    if ref:
        rows = conn.execute(
            "SELECT * FROM declarations WHERE client_ref=? OR invoice_no=? OR decl_no=? "
            "ORDER BY id DESC", (ref, ref, ref)).fetchall()
    result = None
    if ref and len(rows) == 1:
        result = {"id": rows[0]["id"], "decl_no": rows[0]["decl_no"] or "",
                  "status": rows[0]["status"], "reject_reason": rows[0]["reject_reason"] or ""}
    return render_template("lookup.html", ref=ref, rows=rows, result=result,
                           result_json=json.dumps(result, ensure_ascii=False) if result else "null")


@app.route("/codes")
@protected
def codes():
    q = request.args.get("q", "").strip()
    hs = request.args.get("hs", "").strip()
    cat = catalog()
    entries = []
    for code, entry in cat["hs_codes"].items():
        if (q and (q in code or q in entry["name"])) or (hs and hs == code):
            entries.append({"code": code, **entry})
    return render_template("codes.html", q=q, entries=entries, trade_modes=cat["trade_modes"],
                           countries=cat["countries"], ports=cat["ports"])


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="页面不存在"), 404


if __name__ == "__main__":
    init_mock_db()
    port = int(os.environ.get("MOCK_PORT", "8099"))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)
