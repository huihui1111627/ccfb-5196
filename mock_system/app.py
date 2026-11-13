"""Mock customs declaration system (模拟海关申报系统).

Deliberately mimics the awkward parts of a real broker-facing customs portal:

* username/password login with a server-side session that expires (default 20 min)
* trade-mode driven dynamic fields that appear asynchronously after a mode change
* goods lines entered page by page
* a per-item "暂存" action so in-progress work survives a session expiry
* random rejection (退单) with reason codes
* a query page used by the broker for read-back verification

It intentionally does NOT deduplicate declarations.  If a caller submits the
same external reference twice, two declarations exist.  That is what makes the
broker's exactly-once logic meaningful and testable.

Run:  python -m mock_system.app            (port 5055)
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)

from common import catalog


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "data" / "mock.db"

SESSION_COOKIE = "mock_sid"


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    expired_reason TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
    ref TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    header_json TEXT NOT NULL,
    items_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    submitted_at TEXT
);
CREATE TABLE IF NOT EXISTS declarations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    declaration_no TEXT UNIQUE NOT NULL,
    external_ref TEXT NOT NULL,
    header_json TEXT NOT NULL,
    items_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_decl_ref ON declarations(external_ref);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_ref TEXT NOT NULL,
    declaration_no TEXT,
    outcome TEXT NOT NULL,
    reason_code TEXT,
    reason_message TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempt_ref ON attempts(external_ref);
CREATE TABLE IF NOT EXISTS hs_overrides (
    code TEXT PRIMARY KEY,
    name TEXT,
    unit TEXT,
    elements_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"pbkdf2${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), stored)


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    if not conn.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
        conn.execute(
            "INSERT INTO users(username, password_hash, display_name) VALUES(?,?,?)",
            ("admin", hash_password("admin123"), "报关员01"),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app(db_path: str | None = None, **overrides) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = os.environ.get("MOCK_SECRET_KEY", "mock-customs-dev-key")

    db = db_path or os.environ.get("MOCK_DB", str(DEFAULT_DB))
    app.config["DB_PATH"] = db
    app.config["SESSION_SECONDS"] = int(overrides.get(
        "session_seconds",
        os.environ.get("MOCK_SESSION_SECONDS")
        or int(float(os.environ.get("MOCK_SESSION_MINUTES", "20")) * 60),
    ))
    app.config["REJECTION_RATE"] = float(overrides.get(
        "rejection_rate", os.environ.get("MOCK_REJECTION_RATE", "0.25")
    ))
    app.config["FORCE_REJECTION_CODE"] = overrides.get(
        "force_rejection_code", os.environ.get("MOCK_FORCE_REJECTION_CODE", "")
    )
    app.config["FIELD_DELAY_MS"] = int(overrides.get(
        "field_delay_ms", os.environ.get("MOCK_FIELD_DELAY_MS", "400")
    ))
    app.config["DEBUG_ENABLED"] = bool(overrides.get(
        "debug_enabled", os.environ.get("MOCK_DEBUG_ENABLED", "1") == "1"
    ))
    app.config["TESTING"] = bool(overrides.get("testing", False))

    init_db(db)
    app.config["RNG"] = random.Random(int(os.environ.get("MOCK_SEED", "0")) or None)

    # -- db helpers ---------------------------------------------------------
    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = connect(db)
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def log(kind: str, detail: str | dict = "") -> None:
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)
        get_db().execute(
            "INSERT INTO events(ts, kind, detail) VALUES(?,?,?)",
            (now_iso(), kind, detail),
        )
        get_db().commit()

    # -- auth ---------------------------------------------------------------
    def current_user():
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        row = get_db().execute(
            "SELECT s.token, s.expires_at, u.id, u.username, u.display_name "
            "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
            (token,),
        ).fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            get_db().execute(
                "UPDATE sessions SET expired_reason=COALESCE(expired_reason,'timeout') WHERE token=?",
                (token,),
            )
            get_db().commit()
            return None
        return row

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                if request.path.startswith("/api/"):
                    return jsonify({"ok": False, "session_expired": True,
                                    "error": "会话已过期，请重新登录"}), 401
                target = request.full_path if request.method == "GET" else ""
                return redirect(f"/login?expired=1&next={target}")
            g.user = user
            return view(*args, **kwargs)
        return wrapped

    @app.context_processor
    def inject_globals():
        user = None
        try:
            user = current_user()
        except Exception:
            pass
        return {
            "current_user": user,
            "trade_modes": catalog.TRADE_MODES,
            "session_seconds": app.config["SESSION_SECONDS"],
            "catalog": catalog,
            "now": datetime.now(timezone.utc),
        }

    @app.get("/login")
    def login_page():
        if current_user():
            return redirect("/declarations")
        expired = request.args.get("expired") == "1"
        return render_template("login.html", expired=expired,
                               next_url=request.args.get("next", ""))

    @app.post("/login")
    def login_submit():
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            return render_template("login.html", error="用户名或密码错误", expired=False,
                                   next_url=request.form.get("next", "")), 401
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc)
                   + timedelta(seconds=app.config["SESSION_SECONDS"]))
        get_db().execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            (token, row["id"], now_iso(), expires.isoformat(timespec="seconds")),
        )
        get_db().commit()
        log("login", {"user": username, "session_seconds": app.config["SESSION_SECONDS"]})
        resp = redirect(request.form.get("next") or "/declarations")
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Lax")
        return resp

    @app.post("/logout")
    def logout():
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            get_db().execute("DELETE FROM sessions WHERE token=?", (token,))
            get_db().commit()
        resp = redirect("/login")
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # -- pages --------------------------------------------------------------
    @app.get("/")
    def index():
        return redirect("/declarations" if current_user() else "/login")

    @app.get("/declarations")
    @login_required
    def declaration_list():
        declarations = get_db().execute(
            "SELECT * FROM declarations ORDER BY id DESC LIMIT 100"
        ).fetchall()
        drafts = get_db().execute(
            "SELECT ref, header_json, updated_at FROM drafts "
            "WHERE submitted_at IS NULL ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
        return render_template("declarations.html", declarations=declarations,
                               drafts=drafts, parse=json.loads)

    @app.get("/declaration/new")
    @login_required
    def declaration_new():
        ref = request.args.get("ref", "")
        draft = None
        if ref:
            row = get_db().execute("SELECT * FROM drafts WHERE ref=?", (ref,)).fetchone()
            if row and row["submitted_at"] is None:
                draft = {
                    "ref": row["ref"],
                    "header": json.loads(row["header_json"]),
                    "items": json.loads(row["items_json"]),
                    "updated_at": row["updated_at"],
                }
        return render_template(
            "declaration_form.html",
            draft=draft,
            ref=ref or "",
            hs_catalog=catalog.HS_CATALOG,
            element_vocab=catalog.ELEMENT_VOCAB,
            form_labels=catalog.DECLARATION_FIELD_LABELS,
        )

    @app.get("/query")
    @login_required
    def query_page():
        q = request.args.get("q", "").strip()
        decls, attempts = [], []
        if q:
            decls = get_db().execute(
                "SELECT * FROM declarations WHERE declaration_no LIKE ? OR external_ref LIKE ? "
                "ORDER BY id DESC",
                (f"%{q}%", f"%{q}%"),
            ).fetchall()
            attempts = get_db().execute(
                "SELECT * FROM attempts WHERE external_ref LIKE ? OR declaration_no LIKE ? "
                "ORDER BY id DESC",
                (f"%{q}%", f"%{q}%"),
            ).fetchall()
        return render_template("query.html", q=q, declarations=decls, attempts=attempts,
                               parse=json.loads)

    @app.get("/query/<declaration_no>")
    @login_required
    def query_detail(declaration_no):
        row = get_db().execute(
            "SELECT * FROM declarations WHERE declaration_no=?", (declaration_no,)
        ).fetchone()
        if not row:
            return render_template("query.html", q=declaration_no, declarations=[],
                                   attempts=[], parse=json.loads, not_found=True)
        return render_template("declaration_detail.html", d=row,
                               header=json.loads(row["header_json"]),
                               items=json.loads(row["items_json"]),
                               parse=json.loads)

    # -- APIs used by the form ---------------------------------------------
    @app.get("/api/trade-mode/<code>")
    def api_trade_mode(code):
        # Simulate the portal's async lookup: fields appear only after this resolves.
        time.sleep(app.config["FIELD_DELAY_MS"] / 1000.0)
        mode = catalog.TRADE_MODE_BY_CODE.get(code)
        if not mode:
            return jsonify({"ok": False, "error": "未知贸易方式"}), 404
        return jsonify({
            "ok": True,
            "code": mode["code"],
            "name": mode["name"],
            "fields": [
                {"name": f, "label": catalog.FIELD_LABELS[f], "required": True}
                for f in mode["requires"]
            ],
        })

    @app.get("/api/hs/<code>")
    def api_hs(code):
        time.sleep(app.config["FIELD_DELAY_MS"] / 1000.0)
        h = catalog.HS_BY_CODE.get(code)
        if not h:
            return jsonify({"ok": False, "error": "HS编码不在目录中"}), 404
        return jsonify({
            "ok": True, "code": h["code"], "name": h["name"], "unit": h["unit"],
            "elements": [
                {"name": e, "vocab": catalog.ELEMENT_VOCAB.get(e)}
                for e in h["elements"]
            ],
        })

    @app.post("/api/drafts")
    @login_required
    def api_save_header():
        data = request.get_json(force=True, silent=True) or {}
        ref = (data.get("ref") or "").strip()
        header = data.get("header") or {}
        if not ref:
            return jsonify({"ok": False, "error": "缺少外部单号 ref"}), 400
        ts = now_iso()
        row = get_db().execute("SELECT * FROM drafts WHERE ref=?", (ref,)).fetchone()
        if row and row["submitted_at"]:
            return jsonify({"ok": False, "error": "该草稿已提交，不能修改",
                            "submitted": True}), 409
        if row is None:
            get_db().execute(
                "INSERT INTO drafts(ref, user_id, header_json, items_json, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (ref, g.user["id"], json.dumps(header, ensure_ascii=False), "{}", ts, ts),
            )
        else:
            get_db().execute(
                "UPDATE drafts SET header_json=?, updated_at=? WHERE ref=?",
                (json.dumps(header, ensure_ascii=False), ts, ref),
            )
        get_db().commit()
        return jsonify({"ok": True, "ref": ref, "updated_at": ts})

    @app.post("/api/drafts/<path:ref>/items")
    @login_required
    def api_save_item(ref):
        data = request.get_json(force=True, silent=True) or {}
        item = data.get("item") or {}
        header = data.get("header")
        if "item_no" not in item:
            return jsonify({"ok": False, "error": "缺少项号"}), 400
        row = get_db().execute("SELECT * FROM drafts WHERE ref=?", (ref,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "草稿不存在，请先保存表头"}), 404
        if row["submitted_at"]:
            return jsonify({"ok": False, "error": "该草稿已提交", "submitted": True}), 409
        items = json.loads(row["items_json"])
        items[str(item["item_no"])] = item
        ts = now_iso()
        if header:
            get_db().execute(
                "UPDATE drafts SET items_json=?, header_json=?, updated_at=? WHERE ref=?",
                (json.dumps(items, ensure_ascii=False),
                 json.dumps(header, ensure_ascii=False), ts, ref),
            )
        else:
            get_db().execute(
                "UPDATE drafts SET items_json=?, updated_at=? WHERE ref=?",
                (json.dumps(items, ensure_ascii=False), ts, ref),
            )
        get_db().commit()
        return jsonify({"ok": True, "ref": ref, "saved_items": sorted(int(k) for k in items),
                        "updated_at": ts})

    @app.get("/api/drafts/<path:ref>")
    @login_required
    def api_get_draft(ref):
        row = get_db().execute("SELECT * FROM drafts WHERE ref=?", (ref,)).fetchone()
        if not row:
            return jsonify({"ok": False, "exists": False})
        return jsonify({
            "ok": True, "exists": True, "ref": ref,
            "submitted": bool(row["submitted_at"]),
            "header": json.loads(row["header_json"]),
            "items": {int(k): v for k, v in json.loads(row["items_json"]).items()},
            "updated_at": row["updated_at"],
        })

    # -- submit ------------------------------------------------------------
    def validate_for_submit(header: dict, items: dict) -> list[dict]:
        errs = []
        mode = header.get("trade_mode", "")
        if header.get("declaration_type") not in catalog.DECLARATION_TYPES:
            errs.append({"code": "R05", "message": "申报类型缺失或不合法"})
        if mode not in catalog.TRADE_MODE_BY_CODE:
            errs.append({"code": "R05", "message": "贸易方式缺失或不合法"})
        for f in catalog.required_fields(mode):
            if not str(header.get(f, "")).strip():
                errs.append({"code": "R05",
                             "message": f"{catalog.mode_name(mode)}需提供{catalog.FIELD_LABELS[f]}"})
        if not items:
            errs.append({"code": "R01", "message": "至少需要一项商品明细"})
        for ino, item in sorted(items.items(), key=lambda kv: int(kv[0])):
            if not item.get("hs_code"):
                errs.append({"code": "R01", "message": f"项号{ino} HS编码未填"})
                continue
            hs = catalog.HS_BY_CODE.get(item["hs_code"])
            if not hs:
                errs.append({"code": "R02", "message": f"项号{ino} HS编码{item['hs_code']}不在税则目录"})
                continue
            for e in hs["elements"]:
                if not str((item.get("elements") or {}).get(e, "")).strip():
                    errs.append({"code": "R01",
                                 "message": f"{item.get('goods_name', '项号' + str(ino))}申报要素{e}未填"})
            if item.get("unit") not in hs["valid_units"]:
                errs.append({"code": "R03",
                             "message": f"项号{ino}数量单位{item.get('unit')}与法定计量单位不一致"})
            try:
                qty = float(item.get("quantity", 0))
                price = float(item.get("unit_price", 0))
                total = float(item.get("total_price", 0))
                if abs(qty * price - total) > 0.02:
                    errs.append({"code": "R04",
                                 "message": f"项号{ino}总价与单价×数量计算结果不符"})
            except (TypeError, ValueError):
                errs.append({"code": "R04", "message": f"项号{ino}金额格式错误"})
        return errs

    def pick_random_rejection(header, items):
        """Invent a plausible rejection for otherwise-valid data."""
        rng: random.Random = app.config["RNG"]
        force = app.config["FORCE_REJECTION_CODE"]
        if force:
            r = catalog.REJECTION_BY_CODE.get(force) or catalog.REJECTION_REASONS[0]
        else:
            r = rng.choice(catalog.RANDOM_REJECTION_REASONS)
        code = r["code"]
        item = next(iter(items.values())) if items else {}
        hs = catalog.HS_BY_CODE.get(item.get("hs_code", ""), {})
        fill = {
            "element": (hs.get("elements") or ["品牌类型"])[0],
            "hs": item.get("hs_code", "8471301000"),
            "hs_name": hs.get("name", "便携式笔记本电脑"),
            "unit": item.get("unit", "台"),
            "legal_unit": (hs.get("valid_units") or ["台"])[0],
            "total": item.get("total_price", "0"),
            "expected": f"{float(item.get('quantity', 0)) * float(item.get('unit_price', 0)):.2f}"
            if item else "0",
            "goods_name": item.get("goods_name", "该商品"),
            "mode": catalog.mode_name(header.get("trade_mode", "")),
            "field": catalog.FIELD_LABELS.get(
                (catalog.required_fields(header.get("trade_mode", "")) or ["license_no"])[0], "许可证号"),
        }
        try:
            message = r["message"].format(**fill)
        except Exception:
            message = r["message"]
        return {"code": r["code"], "message": message, "category": r["category"], "hint": r["hint"]}

    @app.post("/declaration/submit")
    @login_required
    def submit_declaration():
        ref = request.form.get("ref", "").strip()
        level = request.form.get("confirm_level", "final")
        row = get_db().execute("SELECT * FROM drafts WHERE ref=?", (ref,)).fetchone()
        if not row:
            return render_template("result.html", ok=False, title="提交失败",
                                   message="草稿不存在或会话已丢失", code="E404"), 404
        if row["submitted_at"]:
            return render_template("result.html", ok=False, title="提交失败",
                                   message="该草稿已提交，请重新查询", code="E409"), 409
        header = json.loads(row["header_json"])
        items = json.loads(row["items_json"])
        errs = validate_for_submit(header, items)
        attempt_payload = {"header": header, "items": items, "form_level": level}

        if errs:
            first = errs[0]
            get_db().execute(
                "INSERT INTO attempts(external_ref, outcome, reason_code, reason_message, payload_json, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (ref, "rejected", first["code"], first["message"],
                 json.dumps(attempt_payload, ensure_ascii=False), now_iso()),
            )
            get_db().commit()
            log("submit_rejected", {"ref": ref, "code": first["code"], "message": first["message"]})
            return render_template("result.html", ok=False, title="申报被退回",
                                   message=first["message"], code=first["code"],
                                   hint="请补充或修改后重新提交", ref=ref)

        # Random business rejection, independent of data correctness.
        rejection = None
        if app.config["REJECTION_RATE"] > 0:
            rng: random.Random = app.config["RNG"]
            if rng.random() < app.config["REJECTION_RATE"]:
                rejection = pick_random_rejection(header, items)
        if rejection:
            get_db().execute(
                "INSERT INTO attempts(external_ref, outcome, reason_code, reason_message, payload_json, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (ref, "rejected", rejection["code"],
                 f"{rejection['message']}|{rejection['category']}|{rejection['hint']}",
                 json.dumps(attempt_payload, ensure_ascii=False), now_iso()),
            )
            get_db().commit()
            log("submit_rejected", {"ref": ref, "code": rejection["code"],
                                    "message": rejection["message"]})
            return render_template("result.html", ok=False, title="申报被退回",
                                   message=rejection["message"], code=rejection["code"],
                                   hint=rejection["hint"], ref=ref)

        # Accepted.  No duplicate protection: this mock will happily create a
        # second declaration for the same external ref.
        counts = get_db().execute(
            "SELECT COUNT(*) c FROM declarations WHERE external_ref=?", (ref,)
        ).fetchone()["c"]
        prefix = f"MOCK-{datetime.now().strftime('%Y%m%d')}-"
        seq = get_db().execute(
            "SELECT COUNT(*) c FROM declarations WHERE substr(declaration_no,1,?)=?",
            (len(prefix), prefix),
        ).fetchone()["c"] + 1
        while get_db().execute("SELECT 1 FROM declarations WHERE declaration_no=?",
                               (f"{prefix}{seq:05d}",)).fetchone():
            seq += 1
        declaration_no = f"MOCK-{datetime.now().strftime('%Y%m%d')}-{seq:05d}"
        header_out = dict(header)
        header_out["declaration_no"] = declaration_no
        get_db().execute(
            "INSERT INTO declarations(declaration_no, external_ref, header_json, items_json, created_at, created_by) "
            "VALUES(?,?,?,?,?,?)",
            (declaration_no, ref, json.dumps(header_out, ensure_ascii=False),
             json.dumps(items, ensure_ascii=False), now_iso(), g.user["id"]),
        )
        get_db().execute(
            "INSERT INTO attempts(external_ref, declaration_no, outcome, payload_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (ref, declaration_no, "accepted",
             json.dumps(attempt_payload, ensure_ascii=False), now_iso()),
        )
        get_db().execute("UPDATE drafts SET submitted_at=? WHERE ref=?", (now_iso(), ref))
        get_db().commit()
        log("submit_accepted", {"ref": ref, "declaration_no": declaration_no,
                                "prior_with_same_ref": counts})
        return render_template("result.html", ok=True, title="申报成功",
                               declaration_no=declaration_no, ref=ref,
                               items_count=len(items))

    # -- machine-readable APIs (query / reconciliation) --------------------
    @app.get("/api/query")
    @login_required
    def api_query():
        q = request.args.get("q", "").strip()
        ref = request.args.get("external_ref", "").strip()
        decls = []
        attempts = []
        if q or ref:
            pattern = f"%{q or ref}%"
            decls = get_db().execute(
                "SELECT * FROM declarations WHERE declaration_no LIKE ? OR external_ref LIKE ? "
                "ORDER BY id DESC",
                (pattern, pattern),
            ).fetchall()
            attempts = get_db().execute(
                "SELECT * FROM attempts WHERE external_ref LIKE ? OR declaration_no LIKE ? "
                "ORDER BY id DESC", (pattern, pattern),
            ).fetchall()
        return jsonify({
            "ok": True,
            "declarations": [{
                "declaration_no": r["declaration_no"],
                "external_ref": r["external_ref"],
                "created_at": r["created_at"],
                "header": json.loads(r["header_json"]),
                "items": json.loads(r["items_json"]),
            } for r in decls],
            "attempts": [{
                "id": r["id"], "external_ref": r["external_ref"],
                "declaration_no": r["declaration_no"], "outcome": r["outcome"],
                "reason_code": r["reason_code"], "reason_message": r["reason_message"],
                "created_at": r["created_at"],
            } for r in attempts],
        })

    @app.get("/api/declarations/<declaration_no>")
    @login_required
    def api_declaration(declaration_no):
        r = get_db().execute("SELECT * FROM declarations WHERE declaration_no=?",
                             (declaration_no,)).fetchone()
        if not r:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({
            "ok": True, "declaration_no": r["declaration_no"],
            "external_ref": r["external_ref"], "created_at": r["created_at"],
            "header": json.loads(r["header_json"]),
            "items": json.loads(r["items_json"]),
        })

    @app.get("/api/hs-catalog")
    def api_hs_catalog():
        return jsonify({"ok": True, "catalog": catalog.HS_CATALOG,
                        "element_vocab": catalog.ELEMENT_VOCAB})

    # -- debug APIs (mock only) --------------------------------------------
    def debug_only(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not app.config["DEBUG_ENABLED"]:
                return jsonify({"ok": False, "error": "debug disabled"}), 403
            return view(*args, **kwargs)
        return wrapped

    @app.post("/api/debug/expire-sessions")
    @debug_only
    def debug_expire_sessions():
        get_db().execute(
            "UPDATE sessions SET expires_at=? WHERE expires_at > ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds"),
             now_iso()),
        )
        get_db().commit()
        log("debug_expire_sessions", "")
        return jsonify({"ok": True, "expired": get_db().total_changes})

    @app.post("/api/debug/reset")
    @debug_only
    def debug_reset():
        for table in ("declarations", "attempts", "drafts", "sessions", "events"):
            get_db().execute(f"DELETE FROM {table}")
        get_db().commit()
        log("debug_reset", "")
        return jsonify({"ok": True})

    @app.post("/api/debug/config")
    @debug_only
    def debug_config():
        data = request.get_json(force=True, silent=True) or {}
        if "rejection_rate" in data:
            app.config["REJECTION_RATE"] = float(data["rejection_rate"])
        if "force_rejection_code" in data:
            app.config["FORCE_REJECTION_CODE"] = data["force_rejection_code"] or ""
        if "field_delay_ms" in data:
            app.config["FIELD_DELAY_MS"] = int(data["field_delay_ms"])
        if "session_seconds" in data:
            app.config["SESSION_SECONDS"] = int(data["session_seconds"])
        if "seed" in data:
            app.config["RNG"] = random.Random(int(data["seed"]))
        log("debug_config", data)
        return jsonify({"ok": True, "config": {
            "rejection_rate": app.config["REJECTION_RATE"],
            "force_rejection_code": app.config["FORCE_REJECTION_CODE"],
            "field_delay_ms": app.config["FIELD_DELAY_MS"],
            "session_seconds": app.config["SESSION_SECONDS"],
        }})

    @app.get("/api/stats")
    def api_stats():
        c = get_db()
        return jsonify({
            "ok": True,
            "declarations": c.execute("SELECT COUNT(*) c FROM declarations").fetchone()["c"],
            "attempts": c.execute("SELECT COUNT(*) c FROM attempts").fetchone()["c"],
            "accepted": c.execute("SELECT COUNT(*) c FROM attempts WHERE outcome='accepted'").fetchone()["c"],
            "rejected": c.execute("SELECT COUNT(*) c FROM attempts WHERE outcome='rejected'").fetchone()["c"],
            "drafts": c.execute("SELECT COUNT(*) c FROM drafts WHERE submitted_at IS NULL").fetchone()["c"],
            "duplicate_refs": [dict(r) for r in c.execute(
                "SELECT external_ref, COUNT(*) n FROM declarations GROUP BY external_ref HAVING n>1"
            ).fetchall()],
        })

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "not found"}), 404
        return render_template("result.html", ok=False, title="页面不存在",
                               message="请求的页面不存在", code="E404"), 404

    return app


def main():
    port = int(os.environ.get("MOCK_PORT", "5055"))
    app = create_app()
    print(f"Mock customs system: http://127.0.0.1:{port}  (admin/admin123)")
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False,
            use_reloader=False)


if __name__ == "__main__":
    main()
