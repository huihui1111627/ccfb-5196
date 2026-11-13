"""Web console for the declarant automation system."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, send_from_directory, session, url_for)

from declarant.common import (CATALOG_PATH, add_to_daily, catalog, connect, data_dir,
                              ensure_dirs, get_setting, init_db, now_iso, seed_settings,
                              set_setting, today_str, trade_mode_fields)
from declarant.importer.service import (batch_report, import_file, load_declaration,
                                       revalidate, save_upload)
from declarant.worker import engine, reconcile, rejections
from declarant.worker.client import MockCustomsClient
from declarant.worker.engine import Recorder, log_event, open_session, close_session, touch_session

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("DECLARANT_SECRET", "declarant-console-local-dev")
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024

ensure_dirs()
init_db()
_conn = connect()
seed_settings(_conn)
_conn.commit()
_conn.close()

WORKER_STATE: dict[str, Any] = {"thread": None, "daemon": None, "started_at": None,
                                "mode": None, "decl_id": None, "last_result": None}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def status_label(status: str) -> str:
    return {"queued": "待录入", "validated": "已校验", "hold": "校验未通过",
            "recording": "录入中", "submitted": "已申报", "approved": "审核通过",
            "rejected": "已退单", "abnormal": "异常"}.get(status, status)


STATUS_LABELS = {
    "queued": "待录入", "validated": "已校验", "hold": "校验未通过",
    "recording": "录入中", "submitted": "已申报", "approved": "审核通过",
    "rejected": "已退单", "abnormal": "异常",
}


@app.context_processor
def inject():
    return {"status_labels": STATUS_LABELS, "status_label": status_label,
            "catalog": catalog(), "now": datetime.now(), "now_str": now_iso(),
            "today": today_str(),
            "worker_state": worker_snapshot(),
            "fromjson": _fromjson, "next_seq_of": _next_seq_of}


def _next_seq_of(payload: Any) -> Any:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except json.JSONDecodeError:
            return "—"
    if not isinstance(payload, dict):
        return "—"
    val = payload.get("next_seq")
    return f"第 {val} 项" if val else "—"


def _fromjson(raw: Any) -> list[Any]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else [val]
    except (TypeError, json.JSONDecodeError):
        return []


def worker_snapshot() -> dict[str, Any]:
    thread = WORKER_STATE.get("thread")
    running = bool(thread and thread.is_alive())
    return {"running": running, "mode": WORKER_STATE.get("mode"),
            "decl_id": WORKER_STATE.get("decl_id"),
            "started_at": WORKER_STATE.get("started_at"),
            "last_result": WORKER_STATE.get("last_result")}


def _worker_settings() -> dict[str, Any]:
    conn = db()
    return {
        "base_url": get_setting(conn, "mock_base_url", "http://127.0.0.1:8099"),
        "username": get_setting(conn, "mock_username", "demo"),
        "password": get_setting(conn, "mock_password", "demo123"),
        "headless": get_setting(conn, "headless", True),
        "poll": get_setting(conn, "poll_seconds", 2.0),
    }


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------
@app.route("/")
def dashboard():
    conn = db()
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM declarations GROUP BY status").fetchall()}
    today = conn.execute("SELECT * FROM daily_stats WHERE day=?", (today_str(),)).fetchone()
    recent = conn.execute("SELECT * FROM declarations ORDER BY id DESC LIMIT 8").fetchall()
    open_rej = conn.execute("SELECT COUNT(*) n FROM rejections WHERE status='open'").fetchone()["n"]
    open_diff = conn.execute("SELECT COUNT(DISTINCT declaration_id) n FROM reconciliation "
                             "WHERE status='open'").fetchone()["n"]
    batches = conn.execute("SELECT * FROM import_batches ORDER BY id DESC LIMIT 5").fetchall()
    success_rate = None
    if today:
        total = today["approved_count"] + today["rejected_count"]
        success_rate = round(today["approved_count"] / total * 100, 1) if total else None
    return render_template("dashboard.html", counts=counts, today=today,
                           recent=recent, open_rej=open_rej, open_diff=open_diff,
                           batches=batches, success_rate=success_rate)


# --------------------------------------------------------------------------
# import + queue
# --------------------------------------------------------------------------
@app.route("/queue")
def queue():
    conn = db()
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    sql = ("SELECT d.*, (SELECT COUNT(*) FROM declaration_items i WHERE i.declaration_id=d.id) "
           "AS item_count FROM declarations d WHERE 1=1")
    args: list[Any] = []
    if status:
        sql += " AND d.status=?"
        args.append(status)
    else:
        sql += " AND d.status IN ('queued','validated','hold','recording','abnormal')"
    if q:
        sql += " AND (d.invoice_no LIKE ? OR d.client_ref LIKE ? OR d.contract_no LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY d.id DESC LIMIT 300"
    rows = conn.execute(sql, args).fetchall()
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM declarations GROUP BY status").fetchall()}
    batches = conn.execute("SELECT * FROM import_batches ORDER BY id DESC LIMIT 12").fetchall()
    return render_template("queue.html", rows=rows, status=status, q=q, counts=counts,
                           batches=batches)


@app.route("/import", methods=["POST"])
def do_import():
    files = request.files.getlist("files")
    if not files:
        flash("请选择要导入的 Excel 或 PDF 文件", "err")
        return redirect(url_for("queue"))
    results = []
    for f in files:
        if not f or not f.filename:
            continue
        path = save_upload(f.filename, f.read())
        results.append({"filename": f.filename, **import_file(path, f.filename)})
    total_imported = sum(r.get("imported", 0) for r in results)
    total_dup = sum(r.get("duplicate", 0) for r in results)
    total_bad = sum(r.get("invalid", 0) for r in results)
    flash(f"导入完成：新建 {total_imported} 票，重复跳过 {total_dup} 票，"
          f"校验未通过 {total_bad} 票", "ok" if total_bad == 0 else "warn")
    first_batch = next((r["batch_id"] for r in results if r.get("batch_id")), None)
    if first_batch:
        return redirect(url_for("import_report", batch_id=first_batch))
    return redirect(url_for("queue"))


@app.route("/import/<int:batch_id>")
def import_report(batch_id: int):
    rep = batch_report(batch_id)
    if not rep:
        abort(404)
    return render_template("import_report.html", batch=rep)


@app.route("/declarations/<int:decl_id>")
def declaration_detail(decl_id: int):
    d = load_declaration(decl_id)
    if not d:
        abort(404)
    issues = d.get("issues") or []
    diff = reconcile.latest_diff(decl_id)
    conn = db()
    rej = conn.execute("SELECT * FROM rejections WHERE declaration_id=? ORDER BY id DESC",
                       (decl_id,)).fetchall()
    logs = conn.execute("SELECT * FROM job_logs WHERE declaration_id=? ORDER BY id DESC LIMIT 80",
                        (decl_id,)).fetchall()
    job = engine.get_job(decl_id)
    batch = conn.execute("SELECT * FROM import_batches WHERE id=?", (d.get("batch_id"),)).fetchone()
    open_rejections = [r for r in rej if r["status"] == "open"]
    return render_template("declaration.html", d=d, issues=issues, diff=diff,
                           rejections=rej, open_rejections=open_rejections,
                           logs=logs, job=job, batch=batch)


@app.route("/declarations/<int:decl_id>/edit", methods=["GET", "POST"])
def declaration_edit(decl_id: int):
    d = load_declaration(decl_id)
    if not d:
        abort(404)
    if request.method == "POST":
        conn = db()
        try:
            form = request.form
            header_fields = ["invoice_no", "contract_no", "port_code", "transport_mode",
                             "vessel_voyage", "trade_country", "dest_country", "currency",
                             "packing_type", "consignee", "consignor"]
            for f in header_fields:
                if f in form:
                    conn.execute(f"UPDATE declarations SET {f}=? WHERE id=?",
                                 (form.get(f, "").strip(), decl_id))
            for f in ("exchange_rate", "package_count", "gross_weight", "net_weight",
                      "total_amount"):
                if f in form:
                    try:
                        conn.execute(f"UPDATE declarations SET {f}=? WHERE id=?",
                                     (float(form.get(f) or 0), decl_id))
                    except ValueError:
                        pass
            linked = json.loads(d.get("linked_json") or "{}")
            for spec in trade_mode_fields(d.get("trade_mode", "")):
                name = spec["name"]
                if name in form:
                    linked[name] = form.get(name, "").strip()
            conn.execute("UPDATE declarations SET linked_json=? WHERE id=?",
                         (json.dumps(linked, ensure_ascii=False), decl_id))
            for it in d["items"]:
                seq = it["seq_no"]
                prefix = f"item_{seq}_"
                for f in ("hs_code", "goods_name", "specification", "brand", "model", "unit",
                          "legal_unit", "origin_country"):
                    if prefix + f in form:
                        conn.execute(
                            f"UPDATE declaration_items SET {f}=?, updated_at=? "
                            f"WHERE declaration_id=? AND seq_no=?",
                            (form.get(prefix + f, "").strip(), now_iso(), decl_id, seq))
                for f in ("quantity", "legal_quantity", "second_quantity", "unit_price",
                          "total_price"):
                    if prefix + f in form:
                        try:
                            val = float(form.get(prefix + f) or 0)
                        except ValueError:
                            continue
                        conn.execute(
                            f"UPDATE declaration_items SET {f}=?, updated_at=? "
                            f"WHERE declaration_id=? AND seq_no=?",
                            (val, now_iso(), decl_id, seq))
                elements = it.get("elements") or {}
                for spec in catalog()["hs_codes"].get(it["hs_code"], {}).get("elements", []):
                    field = f"{prefix}el_{spec['key']}"
                    if field in form:
                        elements[spec["key"]] = form.get(field, "").strip()
                conn.execute("UPDATE declaration_items SET elements=?, updated_at=? "
                             "WHERE declaration_id=? AND seq_no=?",
                             (json.dumps(elements, ensure_ascii=False), now_iso(), decl_id, seq))
            conn.commit()
        finally:
            pass
        issues = revalidate(decl_id)
        after = load_declaration(decl_id)
        blocking = [i for i in issues if i.get("severity") == "error"]
        if after and after["status"] == "rejected" and not blocking:
            # a rejected declaration edited by hand goes straight back to the queue;
            # the system-side declaration number is kept, so this is a resubmission
            conn.execute("UPDATE declarations SET status='validated', updated_at=? WHERE id=?",
                         (now_iso(), decl_id))
            conn.commit()
            engine.upsert_job(decl_id, {"status": "pending", "attempt": 0, "step": None,
                                        "note": None, "finished_at": None})
            flash("已保存，校验通过，已重新加入录入队列（沿用原报关单号）", "ok")
        else:
            flash("已保存并重新校验" + ("" if not issues else f"，仍有 {len(issues)} 条提示"), "ok")
        return redirect(url_for("declaration_detail", decl_id=decl_id))
    return render_template("declaration_edit.html", d=d,
                           header_fields=["invoice_no", "contract_no", "port_code",
                                          "transport_mode", "vessel_voyage", "trade_country",
                                          "dest_country", "currency", "packing_type",
                                          "consignee", "consignor"],
                           linked_fields=trade_mode_fields(d.get("trade_mode", "")))


@app.route("/declarations/<int:decl_id>/validate", methods=["POST"])
def validate_decl(decl_id: int):
    issues = revalidate(decl_id)
    d = load_declaration(decl_id)
    if d and d["status"] == "validated":
        flash("校验通过，已加入待录队列", "ok")
    else:
        flash(f"校验仍有 {len(issues)} 条问题", "err")
    return redirect(request.referrer or url_for("declaration_detail", decl_id=decl_id))


@app.route("/declarations/<int:decl_id>/enqueue", methods=["POST"])
def enqueue(decl_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM declarations WHERE id=?", (decl_id,)).fetchone()
    if not row:
        abort(404)
    issues = revalidate(decl_id)
    d = load_declaration(decl_id)
    if d["status"] == "hold":
        flash("校验未通过，无法入队", "err")
        return redirect(request.referrer or url_for("queue"))
    conn.execute("UPDATE declarations SET status='validated', updated_at=? WHERE id=?",
                 (now_iso(), decl_id))
    conn.commit()
    engine.upsert_job(decl_id, {"status": "pending", "step": None, "note": None})
    flash("已加入录入队列", "ok")
    return redirect(request.referrer or url_for("queue"))


@app.route("/declarations/<int:decl_id>/reset", methods=["POST"])
def reset_decl(decl_id: int):
    """Resend an abnormal declaration to the queue (does not touch the customs side)."""
    conn = db()
    row = conn.execute("SELECT status FROM declarations WHERE id=?", (decl_id,)).fetchone()
    if not row:
        abort(404)
    if row["status"] not in ("abnormal", "hold", "validated", "queued", "rejected"):
        flash("只有异常/校验/待录状态可以重排队", "err")
        return redirect(url_for("declaration_detail", decl_id=decl_id))
    conn.execute("UPDATE declarations SET status='validated', updated_at=? WHERE id=?",
                 (now_iso(), decl_id))
    conn.commit()
    engine.upsert_job(decl_id, {"status": "pending", "attempt": 0, "step": None,
                                "note": None, "finished_at": None})
    flash("已重新排队", "ok")
    return redirect(url_for("declaration_detail", decl_id=decl_id))


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
@app.route("/jobs")
def jobs():
    conn = db()
    rows = conn.execute(
        "SELECT j.*, d.invoice_no, d.status AS decl_status, d.mock_decl_id AS decl_mock_id "
        "FROM jobs j JOIN declarations d ON d.id=j.declaration_id "
        "ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 WHEN 'failed' THEN 2"
        " ELSE 3 END, j.updated_at DESC LIMIT 200").fetchall()
    logs = conn.execute("SELECT l.*, d.invoice_no FROM job_logs l LEFT JOIN declarations d "
                        "ON d.id=l.declaration_id ORDER BY l.id DESC LIMIT 120").fetchall()
    sessions = conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 20").fetchall()
    return render_template("jobs.html", rows=rows, logs=logs, sessions=sessions,
                           state=worker_snapshot())


@app.route("/jobs/logs")
def job_logs_api():
    conn = db()
    since = request.args.get("since", type=int, default=0)
    rows = conn.execute(
        "SELECT l.*, d.invoice_no FROM job_logs l LEFT JOIN declarations d "
        "ON d.id=l.declaration_id WHERE l.id>? ORDER BY l.id DESC LIMIT 100", (since,)).fetchall()
    return jsonify({"logs": [dict(r) for r in rows],
                    "max_id": rows[0]["id"] if rows else since,
                    "worker": worker_snapshot()})


@app.route("/jobs/<int:job_id>/cancel", methods=["POST"])
def cancel_job(job_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        abort(404)
    conn.execute("UPDATE jobs SET status='cancelled', finished_at=?, updated_at=? WHERE id=?",
                 (now_iso(), now_iso(), job_id))
    if row["declaration_id"]:
        conn.execute("UPDATE declarations SET status='validated', updated_at=? WHERE id=?",
                     (now_iso(), row["declaration_id"]))
    conn.commit()
    flash("任务已取消", "ok")
    return redirect(url_for("jobs"))


@app.route("/worker/start", methods=["POST"])
def worker_start():
    snap = worker_snapshot()
    if snap["running"]:
        flash("录入进程已在运行", "warn")
        return redirect(url_for("jobs"))
    mode = request.form.get("mode", "daemon")
    decl_id = request.form.get("decl_id", type=int)
    settings = _worker_settings()
    headless = request.form.get("headless", "1") == "1"
    if not request.form.get("skip_recovery"):
        engine.recover_stale()
    if mode == "single" and decl_id:
        def _run_single():
            WORKER_STATE.update({"mode": "single", "decl_id": decl_id,
                                 "started_at": now_iso(), "last_result": None})
            try:
                result = engine.run_single(decl_id, headless=headless,
                                           base_url=settings["base_url"],
                                           username=settings["username"],
                                           password=settings["password"])
                WORKER_STATE["last_result"] = result
            finally:
                WORKER_STATE["mode"] = None
        t = threading.Thread(target=_run_single, name="worker-single", daemon=True)
    else:
        def _run_daemon():
            WORKER_STATE.update({"mode": "daemon", "decl_id": None,
                                 "started_at": now_iso(), "last_result": None})
            daemon = engine.WorkerDaemon(base_url=settings["base_url"],
                                         username=settings["username"],
                                         password=settings["password"],
                                         poll_seconds=float(settings["poll"]),
                                         headless=headless)
            WORKER_STATE["daemon"] = daemon
            try:
                daemon.run_forever()
            finally:
                WORKER_STATE["mode"] = None
        t = threading.Thread(target=_run_daemon, name="worker-daemon", daemon=True)
    WORKER_STATE["thread"] = t
    WORKER_STATE["started_at"] = now_iso()
    t.start()
    flash("录入进程已启动" + ("（单票）" if mode == "single" else "（队列）"), "ok")
    return redirect(url_for("jobs"))


@app.route("/worker/stop", methods=["POST"])
def worker_stop():
    daemon = WORKER_STATE.get("daemon")
    if daemon:
        daemon.stop()
    flash("已发出停止指令，当前票处理完后退出（不会中断提交动作）", "warn")
    return redirect(url_for("jobs"))


@app.route("/worker/status")
def worker_status():
    return jsonify(worker_snapshot())


# --------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------
@app.route("/reconciliation")
def reconciliation_page():
    conn = db()
    only_diff = request.args.get("only_diff", "1") == "1"
    runs = conn.execute(
        "SELECT r.*, d.invoice_no, d.status AS decl_status, d.reject_reason "
        "FROM reconciliation_run r JOIN declarations d ON d.id=r.declaration_id "
        "ORDER BY r.id DESC LIMIT 200").fetchall()
    if only_diff:
        runs = [r for r in runs if r["mismatched"] > 0]
    totals = conn.execute(
        "SELECT COUNT(*) runs, SUM(CASE WHEN mismatched=0 THEN 1 ELSE 0 END) clean,"
        " SUM(mismatched) diffs FROM reconciliation_run").fetchone()
    return render_template("reconciliation.html", runs=runs, only_diff=only_diff,
                           totals=totals)


@app.route("/reconciliation/<int:decl_id>")
def reconciliation_detail(decl_id: int):
    d = load_declaration(decl_id)
    if not d:
        abort(404)
    conn = db()
    run = conn.execute("SELECT * FROM reconciliation_run WHERE declaration_id=? "
                       "ORDER BY id DESC LIMIT 1", (decl_id,)).fetchone()
    snapshot = json.loads(run["snapshot"]) if run else {}
    rows = reconcile.compare(snapshot.get("source", {}), snapshot.get("system", {})) \
        if snapshot else []
    source = snapshot.get("source", {})
    system = snapshot.get("system", {})
    return render_template("reconciliation_detail.html", d=d, rows=rows, run=run,
                           source=source, system=system)


@app.route("/jobs/<int:job_id>/retry", methods=["POST"])
def retry_job(job_id: int):
    """Send a failed/cancelled job back to the queue."""
    conn = db()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        abort(404)
    decl = conn.execute("SELECT status FROM declarations WHERE id=?",
                        (row["declaration_id"],)).fetchone()
    if decl and decl["status"] in ("approved", "submitted", "recording"):
        flash("该单已提交或正在录入，不能重试", "err")
        return redirect(url_for("jobs"))
    conn.execute("UPDATE jobs SET status='pending', attempt=0, note=NULL, finished_at=NULL, "
                 "updated_at=? WHERE id=?", (now_iso(), job_id))
    conn.execute("UPDATE declarations SET status='validated', updated_at=? WHERE id=?",
                 (now_iso(), row["declaration_id"]))
    conn.commit()
    flash("任务已重新排队", "ok")
    return redirect(url_for("jobs"))


@app.route("/reconciliation/<int:decl_id>/rerun", methods=["POST"])
def reconciliation_rerun(decl_id: int):
    d = load_declaration(decl_id)
    if not d or not d.get("mock_decl_id"):
        flash("该单尚无系统单号，无法回读比对", "err")
        return redirect(url_for("reconciliation_page"))
    settings = _worker_settings()
    client = MockCustomsClient(settings["base_url"], settings["username"],
                               settings["password"], headless=settings["headless"])
    try:
        client.start()
        client.login()
        overview_rb = client.read_back(str(d["mock_decl_id"]))
        status = {"审核通过": "approved", "已退单": "rejected", "已申报": "submitted",
                  "草稿": "draft"}.get(overview_rb["status_text"], "unknown")
        actual = {"header": dict(overview_rb["header"]), "items": overview_rb["items"]}
        source = reconcile.build_source_snapshot(d)
        source["header"]["trade_mode"] = d["trade_mode"]
        actual["header"]["trade_mode"] = d["trade_mode"]
        rows = reconcile.compare(source, actual)
        reconcile.save_comparison(decl_id, str(d["mock_decl_id"]), rows,
                                  {"source": source, "system": actual, "decl_no": overview_rb["decl_no"]})
        mismatched = sum(1 for r in rows if not r["match"])
        flash(f"回读比对完成：{len(rows)} 个字段，不一致 {mismatched} 个",
              "ok" if mismatched == 0 else "warn")
    except Exception as exc:  # noqa: BLE001
        flash(f"回读失败：{exc}", "err")
    finally:
        client.stop()
    return redirect(url_for("reconciliation_detail", decl_id=decl_id))


@app.route("/reconciliation/<int:decl_id>/fix", methods=["POST"])
def reconciliation_fix(decl_id: int):
    """Take the customs system's value back into the local record (for diff rows
    where the system is authoritative) or mark the diff as accepted."""
    conn = db()
    path = request.form.get("field_path", "")
    action = request.form.get("action", "accept")
    if action == "accept":
        conn.execute("UPDATE reconciliation SET status='accepted' WHERE declaration_id=? AND field_path=?",
                     (decl_id, path))
        conn.commit()
        flash("差异已标记为已处理", "ok")
    return redirect(url_for("reconciliation_detail", decl_id=decl_id))


# --------------------------------------------------------------------------
# rejections
# --------------------------------------------------------------------------
@app.route("/rejections")
def rejections_page():
    conn = db()
    status = request.args.get("status", "open")
    q = "SELECT r.*, d.invoice_no, d.trade_mode, d.status AS decl_status FROM rejections r " \
        "JOIN declarations d ON d.id=r.declaration_id"
    args: list[Any] = []
    if status:
        q += " WHERE r.status=?"
        args.append(status)
    q += " ORDER BY r.id DESC LIMIT 200"
    rows = conn.execute(q, args).fetchall()
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM rejections GROUP BY status").fetchall()}
    cats = {r["category"]: r["n"] for r in conn.execute(
        "SELECT category, COUNT(*) n FROM rejections GROUP BY category").fetchall()}
    return render_template("rejections.html", rows=rows, status=status, counts=counts, cats=cats,
                           category_labels=rejections.CATEGORY_LABELS)


@app.route("/rejections/<int:rej_id>/repair", methods=["POST"])
def rejection_repair(rej_id: int):
    """Auto-apply a deterministic fix and re-open the declaration for re-submission."""
    conn = db()
    row = conn.execute("SELECT * FROM rejections WHERE id=?", (rej_id,)).fetchone()
    if not row:
        abort(404)
    d = load_declaration(row["declaration_id"])
    parsed = rejections.parse_reason(row["raw_reason"])
    patch = rejections.auto_repair(d, parsed)
    if not patch:
        flash("该退单原因无法自动修复，请在单据详情中手工修改后再重录", "warn")
        return redirect(url_for("declaration_detail", decl_id=row["declaration_id"]))
    if patch["table"] == "items":
        conn.execute("UPDATE declaration_items SET {}=?, updated_at=? WHERE declaration_id=? "
                     "AND seq_no=?".format(patch["field"]),
                     (patch["value"], now_iso(), row["declaration_id"], patch["seq"]))
    elif patch["table"] == "header":
        conn.execute("UPDATE declarations SET {}=?, updated_at=? WHERE id=?".format(patch["field"]),
                     (patch["value"], now_iso(), row["declaration_id"]))
    elif patch["table"] == "elements":
        it = next(x for x in d["items"] if x["seq_no"] == patch["seq"])
        elements = it.get("elements") or {}
        elements[patch["field"]] = patch["value"]
        conn.execute("UPDATE declaration_items SET elements=?, updated_at=? "
                     "WHERE declaration_id=? AND seq_no=?",
                     (json.dumps(elements, ensure_ascii=False), now_iso(),
                      row["declaration_id"], patch["seq"]))
    conn.execute("UPDATE rejections SET status='resolved', resolved_at=?, updated_at=? WHERE id=?",
                 (now_iso(), now_iso(), rej_id))
    conn.execute("UPDATE declarations SET status='validated', updated_at=? WHERE id=?",
                 (now_iso(), row["declaration_id"]))
    conn.commit()
    engine.upsert_job(row["declaration_id"], {"status": "pending", "step": None, "note": None,
                                              "attempt": 0, "finished_at": None})
    log_event("info", "rejection_repair", patch["description"], decl_id=row["declaration_id"])
    flash(f"已自动修复：{patch['description']}；已重新排队等待重录", "ok")
    return redirect(url_for("rejections_page"))


@app.route("/rejections/<int:rej_id>/resolve", methods=["POST"])
def rejection_resolve(rej_id: int):
    conn = db()
    conn.execute("UPDATE rejections SET status='resolved', resolved_at=?, updated_at=? WHERE id=?",
                 (now_iso(), now_iso(), rej_id))
    conn.commit()
    flash("已标记为处理完成", "ok")
    return redirect(url_for("rejections_page", status=request.args.get("status", "open")))


@app.route("/rejections/<int:rej_id>/requeue", methods=["POST"])
def rejection_requeue(rej_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM rejections WHERE id=?", (rej_id,)).fetchone()
    if not row:
        abort(404)
    decl_id = row["declaration_id"]
    conn.execute("UPDATE declarations SET status='validated', updated_at=? WHERE id=?",
                 (now_iso(), decl_id))
    conn.execute("UPDATE rejections SET status='resolved', resolved_at=?, updated_at=? WHERE id=?",
                 (now_iso(), now_iso(), rej_id))
    conn.commit()
    engine.upsert_job(decl_id, {"status": "pending", "step": None, "note": None,
                                "attempt": 0, "finished_at": None, "progress": 0})
    flash("已重新排队，将沿用原报关单号重录并重新申报", "ok")
    return redirect(url_for("rejections_page"))


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    conn = db()
    if request.method == "POST":
        section = request.form.get("section", "mock")
        if section == "mock":
            set_setting(conn, "mock_base_url", request.form.get("mock_base_url", "").strip())
            set_setting(conn, "mock_username", request.form.get("mock_username", "").strip())
            if request.form.get("mock_password"):
                set_setting(conn, "mock_password", request.form.get("mock_password"))
        elif section == "worker":
            set_setting(conn, "headless", request.form.get("headless", "1") == "1")
            try:
                set_setting(conn, "poll_seconds", float(request.form.get("poll_seconds") or 2))
            except ValueError:
                pass
            set_setting(conn, "worker_concurrency",
                        max(1, int(request.form.get("worker_concurrency") or 1)))
        elif section == "mocktest":
            try:
                rate = max(0.0, min(1.0, float(request.form.get("reject_rate") or 0.2)))
            except ValueError:
                rate = 0.2
            set_setting(conn, "mock_reject_rate", rate)
            base = get_setting(conn, "mock_base_url", "http://127.0.0.1:8099")
            try:
                import urllib.request
                urllib.request.urlopen(f"{base}/api/test/reject-rate?rate={rate}", timeout=3).read()
                flash(f"已同步到 Mock 系统：退单模拟概率 {int(rate * 100)}%", "ok")
            except Exception as exc:  # noqa: BLE001
                flash(f"Mock 系统未响应，仅保存本地配置：{exc}", "warn")
        conn.commit()
        flash("设置已保存", "ok")
        return redirect(url_for("settings_page"))
    values = {
        "mock_base_url": get_setting(conn, "mock_base_url", "http://127.0.0.1:8099"),
        "mock_username": get_setting(conn, "mock_username", "demo"),
        "mock_password": get_setting(conn, "mock_password", "demo123"),
        "headless": get_setting(conn, "headless", True),
        "poll_seconds": get_setting(conn, "poll_seconds", 2.0),
        "worker_concurrency": get_setting(conn, "worker_concurrency", 1),
        "mock_reject_rate": get_setting(conn, "mock_reject_rate", 0.2),
    }
    conn_info = {}
    try:
        import sqlite3
        conn_info = {"db_path": str(conn.execute("PRAGMA database_list").fetchone()["file"]),
                     "screenshots": str(data_dir() / "screenshots")}
    except Exception:
        pass
    return render_template("settings.html", values=values, settings=conn_info,
                           settings_rows=conn.execute(
                               "SELECT key, value, description, updated_at FROM settings "
                               "ORDER BY key").fetchall())


@app.route("/settings/test-connection", methods=["POST"])
def test_connection():
    settings = _worker_settings()
    client = MockCustomsClient(settings["base_url"], settings["username"],
                               settings["password"], headless=True)
    try:
        client.start()
        client.login()
        return jsonify({"ok": True, "message": "连接成功，登录通过",
                        "expires_at": client.current_session_expiry()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"连接失败：{exc}"}), 200
    finally:
        client.stop()


# --------------------------------------------------------------------------
# stats + export
# --------------------------------------------------------------------------
@app.route("/stats")
def stats_page():
    conn = db()
    days = conn.execute("SELECT * FROM daily_stats ORDER BY day DESC LIMIT 30").fetchall()
    series = list(reversed([dict(d) for d in days]))
    return render_template("stats.html", days=days, series=series)


@app.route("/stats/day/<day>")
def stats_day(day: str):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM declarations WHERE substr(updated_at,1,10)=? ORDER BY updated_at DESC",
        (day,)).fetchall()
    stat = conn.execute("SELECT * FROM daily_stats WHERE day=?", (day,)).fetchone()
    return render_template("stats_day.html", rows=rows, day=day, stat=stat)


@app.route("/export/stats.xlsx")
def export_stats():
    return _export_stats()


def _export_stats(day: str | None = None) -> Any:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    conn = db()
    wb = Workbook()
    ws = wb.active
    ws.title = "每日统计"
    headers = ["日期", "导入票数", "提交票数", "审核通过", "退单", "重复拦截", "成功率"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1B4F9C")
    days = conn.execute("SELECT * FROM daily_stats ORDER BY day").fetchall()
    for d in days:
        total = d["approved_count"] + d["rejected_count"]
        rate = round(d["approved_count"] / total, 4) if total else ""
        ws.append([d["day"], d["imported_count"], d["submitted_count"], d["approved_count"],
                   d["rejected_count"], d["duplicate_blocked"], rate])
    for row in ws.iter_rows(min_row=2, min_col=7, max_col=7):
        for cell in row:
            cell.number_format = "0.0%"
    ws2 = wb.create_sheet("报关单明细")
    ws2.append(["日期", "发票号", "报关单号", "贸易方式", "项数", "状态", "退单原因", "更新时间"])
    mock_nos: dict[int, str] = {}
    for cell in ws2[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1B4F9C")
    from declarant.common import connect_mock
    try:
        mconn = connect_mock()
        try:
            mock_nos = {row["id"]: row["decl_no"] for row in
                        mconn.execute("SELECT id, decl_no FROM declarations")}
        finally:
            mconn.close()
    except Exception:  # noqa: BLE001 - export still works without the mock db
        mock_nos = {}
    for r in conn.execute("SELECT * FROM declarations ORDER BY id").fetchall():
        decl_no = ""
        try:
            decl_no = mock_nos.get(int(r["mock_decl_id"] or 0), "")
        except (TypeError, ValueError):
            decl_no = ""
        ws2.append([r["updated_at"][:10], r["invoice_no"], decl_no or (r["mock_decl_id"] or ""),
                    r["trade_mode"], len(load_declaration(r["id"])["items"]),
                    status_label(r["status"]), r["reject_reason"] or "",
                    r["updated_at"]])
    ws3 = wb.create_sheet("退单分析")
    ws3.append(["日期", "发票号", "分类", "原因", "状态"])
    for r in conn.execute("SELECT r.*, d.invoice_no FROM rejections r JOIN declarations d "
                          "ON d.id=r.declaration_id ORDER BY r.id").fetchall():
        ws3.append([r["created_at"][:10], r["invoice_no"],
                    rejections.CATEGORY_LABELS.get(r["category"], r["category"]),
                    r["raw_reason"], "已处理" if r["status"] == "resolved" else "待处理"])
    for sheet in (ws, ws2, ws3):
        for i, col in enumerate(sheet.iter_cols(min_row=1, max_row=1), start=1):
            width = max((len(str(c.value or "")) for c in col), default=10)
            sheet.column_dimensions[get_column_letter(i)].width = min(60, max(12, width + 2))
    export_dir = data_dir() / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"stats_{today_str()}.xlsx"
    wb.save(path)
    return send_file(path, as_attachment=True, download_name=f"报关统计_{today_str()}.xlsx")


@app.route("/export/declarations.xlsx")
def export_declarations():
    return _export_stats()


@app.route("/screenshots/<path:filename>")
def screenshot_file(filename: str):
    return send_from_directory(data_dir() / "screenshots", filename)


@app.route("/api/stats")
def api_stats():
    conn = db()
    days = [dict(d) for d in conn.execute(
        "SELECT * FROM daily_stats ORDER BY day DESC LIMIT 14").fetchall()]
    return jsonify(list(reversed(days)))


@app.route("/api/queue")
def api_queue():
    conn = db()
    rows = conn.execute("SELECT id, invoice_no, status, updated_at FROM declarations "
                        "ORDER BY id DESC LIMIT 20").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})


def main() -> None:
    init_db()
    port = int(os.environ.get("DECLARANT_PORT", "8088"))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
