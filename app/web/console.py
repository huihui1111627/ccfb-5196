"""Broker console (报关单自动录入系统).

Pages: 待录队列 / 录入任务 / 核对差异 / 退单处理 / 设置.
The console is a thin layer over SQLite; all heavy lifting lives in
app.services and app.runner.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from flask import (
    Flask, flash, jsonify, redirect, render_template, request,
    send_file, url_for,
)

from app.config import DATA_DIR, EXPORT_DIR, SCREENSHOT_DIR, ensure_dirs
from app.db import Database, json_loads, now_iso
from app.importer import (
    import_source_file, update_document_header, update_document_items,
)
from app.services import fence, state as state_svc
from app.services.reconcile import verify_document
from app.services.stats import daily_summary, export_daily_excel, overall_stats
from common import catalog

ensure_dirs()

REJECTION_CATEGORY_LABELS = {
    "element": "申报要素", "hs": "HS编码", "unit": "计量单位", "amount": "金额",
    "document": "随附单证", "party": "收发货人", "recovery": "恢复流程", "other": "其他",
}

STATUS_LABELS = {
    "pending": "待校验", "ready": "待录", "queued": "已入队", "submitting": "提交中",
    "needs_fix": "待修正", "submitted": "已申报", "rejected": "已退单",
    "revision": "待重录", "verified": "核对一致", "verified_mismatch": "核对有差异",
    "failed": "失败", "cancelled": "已取消",
}


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def tojson(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


# ---------------------------------------------------------------------------
# Dashboard/queue
# ---------------------------------------------------------------------------
def index():
    db = Database()
    counts = state_svc.status_counts(db)
    batches = db.query(
        "SELECT * FROM batches ORDER BY id DESC LIMIT 10")
    docs = db.query(
        "SELECT d.*, b.source_filename FROM documents d LEFT JOIN batches b "
        "ON b.id=d.batch_id ORDER BY d.sequence_no, d.id LIMIT 60")
    active = db.get_settings().get("runner_active", "")
    recent_runs = db.query(
        "SELECT * FROM runs WHERE mode='auto' ORDER BY id DESC LIMIT 5")
    return render_template("queue.html", counts=counts, batches=batches,
                           docs=docs, active=active, recent_runs=recent_runs)


def document_detail(doc_id: int):
    db = Database()
    doc = db.one("SELECT d.*, b.source_filename FROM documents d LEFT JOIN batches b "
                 "ON b.id=d.batch_id WHERE d.id=?", (doc_id,))
    if not doc:
        return render_template("not_found.html"), 404
    header = json_loads(doc["header_json"])
    items = json_loads(doc["items_json"])
    validation = json_loads(doc["validation_json"]) or {"errors": [], "warnings": []}
    compare = json_loads(doc["compare_json"])
    sub = db.one("SELECT * FROM submissions WHERE doc_id=?", (doc_id,))
    steps = db.query(
        "SELECT * FROM run_steps WHERE doc_id=? AND step != 'state' "
        "ORDER BY id DESC LIMIT 80", (doc_id,))
    rejection = db.one("SELECT * FROM rejections WHERE doc_id=?", (doc_id,))
    attempts = db.query(
        "SELECT * FROM submission_attempts WHERE doc_id=? ORDER BY id", (doc_id,))
    problem_map: dict[int, dict[str, bool]] = {}
    for e in (validation.get("errors") or []):
        if e.get("item_no"):
            problem_map.setdefault(int(e["item_no"]), {})[e.get("field") or ""] = True
    return render_template("document.html", doc=doc, header=header, items=items,
                           validation=validation, compare=compare, sub=sub,
                           steps=steps, rejection=rejection, attempts=attempts,
                           catalog=catalog, problem_fields={},
                           problem_map=problem_map,
                           sub_state_label=STATUS_LABELS)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def runs_page():
    db = Database()
    active = db.get_settings().get("runner_active", "")
    if active.startswith("web:"):
        pid = active.split(":")[1]
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            db.set_setting("runner_active", "")
    runs = db.query(
        "SELECT r.*, (SELECT COUNT(*) FROM run_steps s WHERE s.run_id=r.id "
        "AND s.status='error') AS errs FROM runs AS r WHERE r.mode='auto' "
        "ORDER BY r.id DESC LIMIT 50")
    return render_template("runs.html", runs=runs, stats=overall_stats(db),
                           daily=daily_summary(db))


def run_detail(run_id: int):
    db = Database()
    run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    if not run:
        return render_template("not_found.html"), 404
    steps = db.query(
        "SELECT s.*, d.external_ref FROM run_steps s LEFT JOIN documents d "
        "ON d.id=s.doc_id WHERE s.run_id=? ORDER BY s.id", (run_id,))
    stats = json_loads(run["stats_json"]) or {}
    return render_template("run.html", run=run, steps=steps, stats=stats,
                           shots_root=str(SCREENSHOT_DIR))


def run_start():
    db = Database()
    batch_id = request.form.get("batch_id", type=int)
    doc_ids = request.form.get("doc_ids", "")
    ids = [int(x) for x in doc_ids.split(",") if x.strip().isdigit()] if doc_ids else None
    settings = db.get_settings()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent.parent)
    env["BROKER_DATA_DIR"] = str(DATA_DIR)
    env["BROKER_DB"] = str(Path(db.path))
    env["MOCK_BASE_URL"] = settings.get("mock_base_url", env.get("MOCK_BASE_URL", ""))
    cmd = [sys.executable, "-m", "app.runner.main"]
    if batch_id:
        cmd += ["--batch", str(batch_id)]
    if ids:
        cmd += ["--docs", ",".join(str(i) for i in ids)]
    log_file = Path(DATA_DIR) / "runner.log"
    with open(log_file, "ab") as fh:
        proc = subprocess.Popen(cmd, cwd=str(Path(__file__).resolve().parent.parent.parent),
                                env=env, stdout=fh, stderr=subprocess.STDOUT,
                                start_new_session=True)
    db.set_setting("runner_active", f"web:{proc.pid}:{now_iso()}")
    flash(f"已启动录入任务（进程 {proc.pid}）", "info")
    return redirect(url_for("runs_page"))


def run_stop():
    db = Database()
    active = db.get_settings().get("runner_active", "")
    if active.startswith("web:"):
        pid = active.split(":")[1]
        try:
            os.kill(int(pid), 15)
        except Exception:  # noqa: BLE001
            pass
    db.set_setting("runner_active", "")
    flash("已发送停止信号。正在提交中的票不会被中断，下次启动会先核实。", "warning")
    return redirect(url_for("runs_page"))


def run_log(run_id: int):
    db = Database()
    steps = db.query(
        "SELECT * FROM run_steps WHERE run_id=? ORDER BY id", (run_id,))
    return jsonify([{k: s[k] for k in s.keys()} for s in steps])


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------
def screenshot(path: str):
    """Serve a screenshot by run-relative path, refusing anything outside."""
    root = Path(SCREENSHOT_DIR).resolve()
    try:
        candidate = Path(path)
        # Older rows stored an absolute path; accept it only if it lives in root.
        target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        target.relative_to(root)
    except Exception:
        return "forbidden", 403
    if not target.exists() or not target.is_file():
        return "not found", 404
    return send_file(target, mimetype="image/png")


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def reconcile_page():
    db = Database()
    rows = db.query(
        "SELECT d.*, s.declaration_no FROM documents d "
        "LEFT JOIN submissions s ON s.doc_id=d.id "
        "WHERE d.compare_json IS NOT NULL ORDER BY d.updated_at DESC LIMIT 200")
    parsed = []
    for r in rows:
        c = json_loads(r["compare_json"]) or {}
        parsed.append({"row": r, "compare": c, "diff_count": len(c.get("differences") or [])})
    only_diff = request.args.get("only_diff") == "1"
    if only_diff:
        parsed = [p for p in parsed if p["diff_count"]]
    return render_template("reconcile.html", rows=parsed, only_diff=only_diff)


def reconcile_recheck(doc_id: int):
    db = Database()
    from app.runner.main import build_portal
    settings = db.get_settings()
    portal = build_portal(settings)
    portal.start()
    try:
        portal.login()
        result = verify_document(db, doc_id, portal)
    finally:
        portal.close()
    if result["matched"]:
        state_svc.set_document_state(db, doc_id, "verified", "手动回读一致")
        flash(f"回读一致：{result['checked_fields']} 个字段无差异", "info")
    else:
        state_svc.set_document_state(db, doc_id, "verified_mismatch", "手动回读存在差异")
        flash(f"回读发现 {len(result['differences'])} 处差异", "warning")
    return redirect(url_for("document_detail", doc_id=doc_id))


def reconcile_export():
    db = Database()
    path = export_daily_excel(db, EXPORT_DIR)
    return send_file(path, as_attachment=True)


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------
def rejections_page():
    db = Database()
    status = request.args.get("status", "")
    sql = ("SELECT r.*, d.external_ref, d.header_json, d.items_json, d.status AS doc_status "
           "FROM rejections r JOIN documents d ON d.id=r.doc_id ")
    params: list = []
    if status:
        sql += "WHERE r.status=? "
        params.append(status)
    sql += "ORDER BY r.id DESC LIMIT 200"
    rows = db.query(sql, tuple(params))
    parsed = []
    for r in rows:
        parsed.append({
            "r": r,
            "header": json_loads(r["header_json"]) or {},
            "items": json_loads(r["items_json"]) or [],
        })
    counts = {row["status"]: row["n"] for row in
              db.query("SELECT status, COUNT(*) n FROM rejections GROUP BY status")}
    return render_template("rejections.html", rows=parsed, status=status,
                           counts=counts, catalog=catalog,
                           category_labels=REJECTION_CATEGORY_LABELS,
                           status_label=STATUS_LABELS.get)


def rejection_update(rejection_id: int):
    db = Database()
    rec = db.one("SELECT * FROM rejections WHERE id=?", (rejection_id,))
    if not rec:
        return render_template("not_found.html"), 404
    action = request.form.get("action", "")
    note = request.form.get("note", "")
    handler = request.form.get("handler", "operator")
    if action == "resolve":
        db.execute(
            "UPDATE rejections SET status='resolved', handler=?, resolution_note=?, "
            "updated_at=?, resolved_at=? WHERE id=?",
            (handler, note, now_iso(), now_iso(), rejection_id))
        doc = db.one("SELECT * FROM documents WHERE id=?", (rec["doc_id"],))
        if doc["status"] == "rejected":
            state_svc.set_document_state(db, rec["doc_id"], "revision", "退单已处理")
        flash("退单已标记为已处理，可重新录入", "info")
    elif action == "waive":
        db.execute(
            "UPDATE rejections SET status='waived', handler=?, resolution_note=?, "
            "updated_at=?, resolved_at=? WHERE id=?",
            (handler, note, now_iso(), now_iso(), rejection_id))
        flash("已放弃该票", "warning")
    elif action == "handling":
        db.execute("UPDATE rejections SET status='handling', handler=?, updated_at=? WHERE id=?",
                   (handler, now_iso(), rejection_id))
        flash("已标记为处理中", "info")
    return redirect(url_for("rejections_page"))


def rejection_reentry(doc_id: int):
    """Queue the document for re-entry after a fix."""
    db = Database()
    doc = db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc:
        return render_template("not_found.html"), 404
    if doc["status"] not in ("rejected", "revision", "verified_mismatch", "failed", "needs_fix"):
        flash("该票当前状态不允许重录", "warning")
        return redirect(url_for("rejections_page"))
    validation = json_loads(doc["validation_json"]) or {}
    if validation.get("errors"):
        flash(f"该票仍有 {len(validation['errors'])} 个校验错误，请先在单据详情修正", "warning")
        return redirect(url_for("document_detail", doc_id=doc_id))
    if doc["status"] != "revision":
        state_svc.set_document_state(db, doc_id, "revision", "退单处理后重录")
    state_svc.set_document_state(db, doc_id, "queued", "加入待录队列")
    db.execute(
        "UPDATE rejections SET status='resolved', updated_at=?, resolved_at=? "
        "WHERE doc_id=? AND status IN ('open','handling')",
        (now_iso(), now_iso(), doc_id))
    flash("已加入待录队列，下次录入任务会重新申报", "info")
    return redirect(url_for("document_detail", doc_id=doc_id))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def settings_page():
    db = Database()
    settings = db.get_settings()
    if request.method == "POST":
        for key in ("mock_base_url", "mock_username", "session_minutes",
                    "slow_mo_ms", "max_resubmit", "settle_seconds_after_uncertain",
                    "probe_attempts", "reconcile_strict"):
            if key in request.form:
                db.set_setting(key, request.form[key].strip())
        db.set_setting("headless", "1" if request.form.get("headless") == "1" else "0")
        db.set_setting("verify_after_submit",
                       "1" if request.form.get("verify_after_submit") == "1" else "0")
        db.set_setting("screenshot_all_steps",
                       "1" if request.form.get("screenshot_all_steps") == "1" else "0")
        # Password only overwritten when supplied.
        if request.form.get("mock_password"):
            db.set_setting("mock_password", request.form["mock_password"])
        flash("设置已保存", "info")
        return redirect(url_for("settings_page"))
    mock_status = _probe_mock(settings)
    return render_template("settings.html", settings=settings, mock=mock_status,
                           catalog=catalog)


def _probe_mock(settings: dict) -> dict:
    import urllib.request
    url = settings["mock_base_url"].rstrip("/") + "/login"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return {"ok": resp.status == 200, "url": url, "code": resp.status}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "error": str(exc)}


def settings_test_mock():
    db = Database()
    settings = db.get_settings()
    from app.runner.main import build_portal
    portal = build_portal(settings)
    try:
        portal.login()
        return jsonify({"ok": True, "message": "登录成功", "url": portal.url()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 500
    finally:
        portal.close()


# ---------------------------------------------------------------------------
# API: import + edit + queue
# ---------------------------------------------------------------------------
def api_import():
    db = Database()
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "未选择文件"}), 400
    results = []
    tmp_dir = Path(DATA_DIR) / "uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        if not f.filename:
            continue
        safe_name = Path(f.filename).name
        tmp_path = tmp_dir / f"{now_iso().replace(':', '-')}_{safe_name}"
        f.save(tmp_path)
        try:
            res = import_source_file(db, tmp_path, filename=safe_name)
            results.append({"ok": True, **res})
        except Exception as exc:  # noqa: BLE001
            results.append({"ok": False, "filename": safe_name, "error": str(exc)})
    ok_any = any(r["ok"] for r in results)
    return jsonify({"ok": ok_any, "results": results})


def api_document(doc_id: int):
    db = Database()
    doc = db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({
        "ok": True,
        "document": {k: doc[k] for k in doc.keys()},
        "header": json_loads(doc["header_json"]),
        "items": json_loads(doc["items_json"]),
        "validation": json_loads(doc["validation_json"]),
        "compare": json_loads(doc["compare_json"]),
    })


def api_update_document(doc_id: int):
    db = Database()
    data = request.get_json(force=True, silent=True) or {}
    doc = db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc:
        return jsonify({"ok": False, "error": "not found"}), 404
    if doc["status"] in ("submitted", "verified", "verified_mismatch"):
        return jsonify({"ok": False,
                        "error": "该票已申报，修改会产生与系统不一致，请走差异核对流程"}), 409
    try:
        if data.get("header"):
            update_document_header(db, doc_id, data["header"])
        if data.get("items"):
            update_document_items(db, doc_id, data["items"])
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
    doc = db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
    validation = json_loads(doc["validation_json"])
    if validation.get("errors"):
        state_svc.set_document_state(db, doc_id, "needs_fix", "修正后仍有校验错误",
                                     allow_same=True) if doc["status"] != "needs_fix" else None
    elif doc["status"] == "needs_fix":
        state_svc.set_document_state(db, doc_id, "ready", "修正后校验通过")
    return jsonify({"ok": True, "validation": validation,
                    "items": json_loads(doc["items_json"]),
                    "header": json_loads(doc["header_json"])})


def api_queue():
    db = Database()
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get("document_ids") or []
    batch_id = data.get("batch_id")
    result = state_svc.queue_documents(db, doc_ids=ids or None, batch_id=batch_id)
    return jsonify({"ok": True, **result})


def api_validate_document(doc_id: int):
    db = Database()
    doc = db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify(json_loads(doc["validation_json"]))


def document_reenter(doc_id: int):
    """Re-enter an already-accepted ticket that failed read-back (or was rejected)."""
    db = Database()
    doc = db.one("SELECT d.*, s.state AS fence_state FROM documents d "
                 "LEFT JOIN submissions s ON s.doc_id=d.id WHERE d.id=?", (doc_id,))
    if not doc:
        return render_template("not_found.html"), 404
    if doc["status"] == "rejected" or doc["status"] == "verified_mismatch":
        if doc["status"] == "verified_mismatch":
            # An accepted ticket cannot simply be re-submitted: that WOULD create
            # a duplicate.  The operator must first void/cancel the existing
            # declaration in the customs system and record that decision here.
            flash("该票系统已受理，不能直接重录（会产生重复申报）。请在申报系统作废原单，"
                  "再在「核对差异」页登记后解除。", "error")
            return redirect(url_for("document_detail", doc_id=doc_id))
        state_svc.set_document_state(db, doc_id, "revision", "人工重录")
        state_svc.set_document_state(db, doc_id, "queued", "加入待录队列")
        flash("已加入待录队列", "info")
        return redirect(url_for("document_detail", doc_id=doc_id))
    flash("当前状态不允许重录", "warning")
    return redirect(url_for("document_detail", doc_id=doc_id))


def api_resolve_uncertain(doc_id: int):
    db = Database()
    data = request.get_json(force=True, silent=True) or {}
    resolution = data.get("resolution")
    note = data.get("note", "")
    declaration_no = data.get("declaration_no", "")
    try:
        result = fence.operator_resolve(db, doc_id, resolution, note, declaration_no)
    except fence.FenceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    if resolution == "accepted":
        state_svc.set_document_state(db, doc_id, "submitted", "人工确认系统已受理")
    elif result == "released":
        doc = db.one("SELECT status FROM documents WHERE id=?", (doc_id,))
        if doc["status"] == "failed":
            state_svc.set_document_state(db, doc_id, "queued", "人工确认未提交，可重录")
    return jsonify({"ok": True, "resolution": resolution})


def api_batch_delete(batch_id: int):
    db = Database()
    docs = db.query("SELECT id, status FROM documents WHERE batch_id=?", (batch_id,))
    if any(d["status"] in ("submitting", "submitted", "verified", "verified_mismatch")
           for d in docs):
        return jsonify({"ok": False,
                        "error": "批次内有已申报/提交中的单据，不能删除"}), 409
    with db.tx(immediate=True):
        db.execute("DELETE FROM documents WHERE batch_id=?", (batch_id,))
        db.execute("DELETE FROM batches WHERE id=?", (batch_id,))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app() -> Flask:
    base = Path(__file__).resolve().parent.parent
    app = Flask(__name__, template_folder=str(base / "web" / "templates"),
                static_folder=str(base / "web" / "static"))
    app.secret_key = os.environ.get("BROKER_SECRET", "broker-console-dev")
    app.jinja_env.filters["tojson_pretty"] = lambda v: json.dumps(
        v, ensure_ascii=False, indent=2, default=_json_default)
    def _fromjson(value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:  # noqa: BLE001
                return {}
        return value or {}

    app.jinja_env.filters["fromjson"] = _fromjson
    app.jinja_env.filters["tojson_pretty"] = lambda v: json.dumps(
        v, ensure_ascii=False, indent=2, default=_json_default)
    app.jinja_env.globals.update(
        catalog=catalog, tojson=tojson, now_iso=now_iso,
        state_counts=lambda: state_svc.status_counts(Database()),
        status_label=STATUS_LABELS.get,
        status_counts_map=lambda: state_svc.status_counts(Database()))

    @app.context_processor
    def inject_settings():
        try:
            return {"mock_url": Database().get_settings().get("mock_base_url", "#")}
        except Exception:  # noqa: BLE001
            return {"mock_url": "#"}
    app.add_url_rule("/", "index", index)
    app.add_url_rule("/document/<int:doc_id>", "document_detail", document_detail)
    app.add_url_rule("/runs", "runs_page", runs_page)
    app.add_url_rule("/runs/start", "run_start", run_start, methods=["POST"])
    app.add_url_rule("/runs/stop", "run_stop", run_stop, methods=["POST"])
    app.add_url_rule("/runs/<int:run_id>", "run_detail", run_detail)
    app.add_url_rule("/api/runs/<int:run_id>/log", "run_log", run_log)
    app.add_url_rule("/shots/<path:path>", "screenshot", screenshot)
    app.add_url_rule("/reconcile", "reconcile_page", reconcile_page)
    app.add_url_rule("/reconcile/<int:doc_id>/recheck", "reconcile_recheck",
                     reconcile_recheck, methods=["POST"])
    app.add_url_rule("/reconcile/export", "reconcile_export", reconcile_export)
    app.add_url_rule("/rejections", "rejections_page", rejections_page)
    app.add_url_rule("/rejections/<int:rejection_id>/update", "rejection_update",
                     rejection_update, methods=["POST"])
    app.add_url_rule("/rejections/<int:doc_id>/reentry", "rejection_reentry",
                     rejection_reentry, methods=["POST"])
    app.add_url_rule("/documents/<int:doc_id>/reenter", "document_reenter",
                     document_reenter, methods=["POST"])
    app.add_url_rule("/settings", "settings_page", settings_page, methods=["GET", "POST"])
    app.add_url_rule("/settings/test-mock", "settings_test_mock", settings_test_mock,
                     methods=["POST"])
    app.add_url_rule("/api/import", "api_import", api_import, methods=["POST"])
    app.add_url_rule("/api/documents/<int:doc_id>", "api_document", api_document)
    app.add_url_rule("/api/documents/<int:doc_id>/update", "api_update_document",
                     api_update_document, methods=["POST"])
    app.add_url_rule("/api/documents/<int:doc_id>/validation", "api_validate_document",
                     api_validate_document)
    app.add_url_rule("/api/documents/<int:doc_id>/resolve", "api_resolve_uncertain",
                     api_resolve_uncertain, methods=["POST"])
    app.add_url_rule("/api/queue", "api_queue", api_queue, methods=["POST"])
    app.add_url_rule("/api/batches/<int:batch_id>", "api_batch_delete", api_batch_delete,
                     methods=["DELETE"])
    app.add_url_rule("/healthz", "healthz", lambda: jsonify({"ok": True, "time": now_iso()}))
    return app


app = create_app()


def main() -> None:
    port = int(os.environ.get("CONSOLE_PORT", "5050"))
    print(f"Broker console: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False,
            use_reloader=False)


if __name__ == "__main__":
    main()
