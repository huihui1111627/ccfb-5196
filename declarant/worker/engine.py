"""Recording engine: picks queued declarations and drives the browser.

No-duplicate guarantees
-----------------------
1. ``declarations.fingerprint`` is UNIQUE — the same invoice can only ever be
   imported once.
2. ``submit_ledger`` has UNIQUE(fingerprint, revision) and an ``intent`` row is
   written *before* the browser clicks submit. If the process dies after the
   click, the next run sees the intent and re-queries the customs system
   instead of submitting again.
3. Before every submit the engine queries the system by client_ref. If the
   declaration is already 已申报/审核通过, it never opens the submit page.
4. Mock system enforces UNIQUE(client_ref) and refuses re-submission of an
   approved declaration.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from declarant.common import (add_to_daily, connect, connect_mock, data_dir, ensure_dirs,
                              get_setting, now_iso, seed_settings)
from declarant.importer.service import load_declaration
from declarant.worker import reconcile, rejections
from declarant.worker.client import MockCustomsClient, StepFailed

DEFAULT_REJECTION_CATEGORY = "UNKNOWN"


# --------------------------------------------------------------------------
# small persistence helpers
# --------------------------------------------------------------------------
def log_event(level: str, event: str, message: str = "", *, job_id: int | None = None,
              decl_id: int | None = None, session_id: int | None = None,
              screenshot: str | None = None, detail: Any = None) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO job_logs(job_id,declaration_id,session_id,level,event,message,"
            "screenshot,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (job_id, decl_id, session_id, level, event, message, screenshot,
             json.dumps(detail, ensure_ascii=False) if detail is not None else None, now_iso()))
        conn.commit()
    finally:
        conn.close()


def open_session(label: str = "") -> int:
    conn = connect()
    try:
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO sessions(username,label,status,started_at,last_seen_at,expires_at) "
            "VALUES(?,?,?,?,?,?)", ("demo", label or "worker", "active", ts, ts, ts))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def close_session(session_id: int, status: str = "stopped") -> None:
    conn = connect()
    try:
        conn.execute("UPDATE sessions SET status=?, stopped_at=?, last_seen_at=? WHERE id=?",
                     (status, now_iso(), now_iso(), session_id))
        conn.commit()
    finally:
        conn.close()


def touch_session(session_id: int, expires_at: str | None = None, renewed: bool = False,
                  status: str | None = None) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE sessions SET last_seen_at=?, expires_at=COALESCE(?,expires_at), "
                     "renewed_count=renewed_count+?, status=COALESCE(?,status) WHERE id=?",
                     (now_iso(), expires_at, 1 if renewed else 0, status, session_id))
        conn.commit()
    finally:
        conn.close()


def get_job(decl_id: int, kind: str = "record") -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE declaration_id=? AND kind=?",
                           (decl_id, kind)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload") or "{}")
        except json.JSONDecodeError:
            d["payload"] = {}
        return d
    finally:
        conn.close()


def upsert_job(decl_id: int, values: dict[str, Any], kind: str = "record") -> int:
    conn = connect()
    try:
        ts = now_iso()
        existing = conn.execute("SELECT id FROM jobs WHERE declaration_id=? AND kind=?",
                                (decl_id, kind)).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in values)
            conn.execute(f"UPDATE jobs SET {sets}, updated_at=? WHERE id=?",
                         (*values.values(), ts, existing["id"]))
            job_id = existing["id"]
        else:
            cols = ["declaration_id", "kind", *values.keys(), "created_at", "updated_at"]
            placeholders = ",".join("?" * len(cols))
            cur = conn.execute(f"INSERT INTO jobs({','.join(cols)}) VALUES({placeholders})",
                               (decl_id, kind, *values.values(), ts, ts))
            job_id = cur.lastrowid
        conn.commit()
        return job_id
    finally:
        conn.close()


def set_decl_status(decl_id: int, status: str, **fields: Any) -> None:
    conn = connect()
    try:
        sets = ["status=?", "updated_at=?"]
        args: list[Any] = [status, now_iso()]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            args.append(v)
        args.append(decl_id)
        conn.execute(f"UPDATE declarations SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()
    finally:
        conn.close()


def eligible_declarations(limit: int = 50) -> list[dict[str, Any]]:
    """Declarations ready to record whose job is still pending (not done/cancelled)."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT d.* FROM declarations d "
            "LEFT JOIN jobs j ON j.declaration_id=d.id AND j.kind='record' "
            "WHERE d.status IN ('queued','validated') "
            "AND (j.id IS NULL OR j.status IN ('pending','failed')) "
            "ORDER BY d.id LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _load_job_declaration(decl_id: int) -> dict[str, Any] | None:
    d = load_declaration(decl_id)
    if not d:
        return None
    job = get_job(decl_id)
    if job and job.get("mock_decl_id"):
        d["mock_decl_id"] = job["mock_decl_id"]
    if job:
        payload = job.get("payload") or {}
        if payload.get("next_seq"):
            d["next_seq"] = payload["next_seq"]
    return d


def claim(decl_id: int) -> bool:
    """Atomically move a declaration to 'recording' and claim its job.

    A second worker (another thread or another process) loses the race and
    returns False, so a declaration can never be typed in twice concurrently.
    """
    conn = connect()
    try:
        ts = now_iso()
        cur = conn.execute(
            "UPDATE declarations SET status='recording', updated_at=? "
            "WHERE id=? AND status IN ('queued','validated')", (ts, decl_id))
        if cur.rowcount == 0:
            conn.rollback()
            return False
        conn.execute("INSERT INTO jobs(declaration_id,kind,status,created_at,updated_at) "
                     "VALUES(?,?,?,?,?) ON CONFLICT(declaration_id,kind) DO NOTHING",
                     (decl_id, "record", "pending", ts, ts))
        cur = conn.execute(
            "UPDATE jobs SET status='running', attempt=attempt+1, started_at=?, updated_at=? "
            "WHERE declaration_id=? AND kind='record' AND status IN ('pending','failed')",
            (ts, ts, decl_id))
        if cur.rowcount == 0:
            conn.rollback()
            conn.execute("UPDATE declarations SET status='validated', updated_at=? "
                         "WHERE id=? AND status='recording'", (ts, decl_id))
            conn.commit()
            return False
        conn.commit()
        return True
    finally:
        conn.close()


def recover_stale(minutes: int = 10) -> int:
    """Release declarations whose worker died while recording (stale lease)."""
    conn = connect()
    try:
        cutoff = (datetime.now() - __import__("datetime").timedelta(minutes=minutes)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            "SELECT id FROM declarations WHERE status='recording' AND updated_at<? "
            "AND id NOT IN (SELECT declaration_id FROM jobs WHERE status='running' "
            "AND updated_at>=?)", (cutoff, cutoff)).fetchall()
        ids = [r["id"] for r in rows]
        for did in ids:
            conn.execute("UPDATE declarations SET status='validated', updated_at=? WHERE id=?",
                         (now_iso(), did))
            conn.execute("UPDATE jobs SET status='pending', updated_at=? "
                         "WHERE declaration_id=? AND kind='record' AND status='running'",
                         (now_iso(), did))
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def _save_progress(decl_id: int, *, next_seq: int | None = None, mock_decl_id: str | None = None,
                   step: str | None = None) -> None:
    values: dict[str, Any] = {}
    job = get_job(decl_id) or {}
    payload = job.get("payload") or {}
    if next_seq is not None:
        payload["next_seq"] = next_seq
    if mock_decl_id is not None:
        values["mock_decl_id"] = str(mock_decl_id)
    if payload:
        values["payload"] = json.dumps(payload, ensure_ascii=False)
    if step:
        values["step"] = step
    upsert_job(decl_id, values)
    # refresh the lease timestamp used by recover_stale()
    conn = connect()
    try:
        conn.execute("UPDATE declarations SET updated_at=? WHERE id=?", (now_iso(), decl_id))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# ledger — the duplicate-submission guard
# --------------------------------------------------------------------------
def ensure_intent(decl: dict[str, Any], revision: int, mock_decl_id: str) -> bool:
    """Write a submit intent. Returns True if this run owns the intent."""
    conn = connect()
    try:
        ts = now_iso()
        existing = conn.execute(
            "SELECT * FROM submit_ledger WHERE fingerprint=? AND revision=?",
            (decl["fingerprint"], revision)).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO submit_ledger(declaration_id,fingerprint,revision,phase,mock_decl_id,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (decl["id"], decl["fingerprint"], revision, "intent", mock_decl_id, ts, ts))
        conn.commit()
        return True
    finally:
        conn.close()


def ledger_state(decl: dict[str, Any], revision: int) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM submit_ledger WHERE fingerprint=? AND revision=?",
                           (decl["fingerprint"], revision)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def stale_intent(decl: dict[str, Any], revision: int, seconds: int = 90) -> bool:
    state = ledger_state(decl, revision)
    if not state:
        return True
    try:
        created = datetime.strptime(state["created_at"], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return True
    return (datetime.now() - created).total_seconds() >= seconds


def clear_intent(decl: dict[str, Any], revision: int) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM submit_ledger WHERE fingerprint=? AND revision=? "
                     "AND phase='intent'", (decl["fingerprint"], revision))
        conn.commit()
    finally:
        conn.close()


def ledger_finish(decl: dict[str, Any], revision: int, *, phase: str, decl_no: str = "",
                  outcome: str = "", note: str = "") -> None:
    conn = connect()
    try:
        conn.execute("UPDATE submit_ledger SET phase=?, decl_no=?, outcome=?, note=?, "
                     "updated_at=? WHERE fingerprint=? AND revision=?",
                     (phase, decl_no, outcome, note, now_iso(), decl["fingerprint"], revision))
        conn.commit()
    finally:
        conn.close()


def system_status(client: MockCustomsClient, decl: dict[str, Any]) -> dict[str, Any] | None:
    rows = client.lookup(decl["client_ref"])
    if not rows:
        return None
    return rows[0]


# --------------------------------------------------------------------------
# one declaration
# --------------------------------------------------------------------------
class Recorder:
    def __init__(self, client: MockCustomsClient, session_id: int,
                 stop_event: threading.Event | None = None):
        self.client = client
        self.session_id = session_id
        self.stop_event = stop_event or threading.Event()
        self._renewals_seen = 0

    def record(self, decl_id: int) -> dict[str, Any]:
        decl = _load_job_declaration(decl_id)
        if not decl:
            return {"ok": False, "error": "报关单不存在"}
        job = get_job(decl_id)
        if job and job["status"] in ("done", "cancelled"):
            return {"ok": True, "skipped": True, "status": job["status"]}
        if decl["status"] not in ("queued", "validated"):
            return {"ok": True, "skipped": True, "status": decl["status"]}
        if not claim(decl_id):
            return {"ok": True, "skipped": True, "status": "claimed_by_other"}
        log_event("info", "job_started", f"开始录入 {decl['invoice_no']}", decl_id=decl_id,
                  session_id=self.session_id)
        try:
            result = self._record_inner(decl)
        except StepFailed as exc:
            self._fail(decl_id, exc.step, exc.message, exc.screenshot)
            return {"ok": False, "error": exc.message, "step": exc.step}
        except Exception as exc:  # noqa: BLE001 - browser errors must not kill the daemon
            shot = self.client.screenshot(f"error_{decl['invoice_no']}")
            self._fail(decl_id, "unexpected", f"{type(exc).__name__}: {exc}", shot,
                       detail=traceback.format_exc()[-3000:])
            return {"ok": False, "error": str(exc)}
        return result

    def _record_inner(self, decl: dict[str, Any]) -> dict[str, Any]:
        decl_id = decl["id"]
        client = self.client
        client.ensure_login()
        self._refresh_session()

        # 0) never create twice
        opened = client.open_or_create(decl)
        mock_id = opened["mock_decl_id"]
        _save_progress(decl_id, mock_decl_id=mock_id, step="created")
        set_decl_status(decl_id, "recording", mock_decl_id=str(mock_id))
        log_event("info", "declaration_ready",
                  f"系统单据 ID={mock_id}（{'新建' if opened['created'] else '已存在，复用'}）",
                  decl_id=decl_id, session_id=self.session_id, screenshot=client.screenshot("opened"))
        decl["mock_decl_id"] = mock_id
        if opened["status"] in ("approved", "submitted"):
            return self._finalise_existing(decl, opened)

        # 1) header — follow trade-mode linked fields
        client.fill_header(decl)
        _save_progress(decl_id, step="header_done")
        self._refresh_session()

        # 2) items — resume from the first unfinished line
        # after a rejection the mock already holds the old values, so rows must be
        # rewritten even if HS/quantity still look equal (e.g. a price repair)
        force_rewrite = bool(int(decl.get("reject_attempts") or 0))
        resume_seq = int(decl.get("next_seq") or 1)
        done_seq = {"value": resume_seq - 1}
        if resume_seq > 1:
            log_event("warn", "resume_items", f"从第 {resume_seq} 项续录", decl_id=decl_id,
                      session_id=self.session_id)

        def on_item_done(seq: int) -> None:
            done_seq["value"] = max(done_seq["value"], seq)
            _save_progress(decl_id, next_seq=seq + 1)
            self._refresh_session()

        client.fill_items(decl, start_seq=resume_seq, on_item_done=on_item_done,
                          force=force_rewrite)
        _save_progress(decl_id, next_seq=len(decl["items"]) + 1, step="items_done")
        log_event("info", "items_done", f"{len(decl['items'])} 项明细全部录入",
                  decl_id=decl_id, session_id=self.session_id)

        # 3) submit exactly once
        submit = self._submit_once(decl)
        return self._after_submit(decl, submit)

    # -- submit -----------------------------------------------------------
    def _submit_once(self, decl: dict[str, Any]) -> dict[str, Any]:
        decl_id = decl["id"]
        revision = self._revision_of(decl)
        state = ledger_state(decl, revision)
        if state and state["phase"] in ("submitted", "approved", "rejected"):
            log_event("warn", "submit_already_recorded",
                      f"提交台账显示该版本已提交（{state['outcome']}），不再重复提交",
                      decl_id=decl_id, session_id=self.session_id)
            existing = system_status(self.client, decl)
            if existing:
                return {"outcome": existing["status"], "decl_no": existing["decl_no"],
                        "prevented_duplicate": True, "reason": state.get("note") or "",
                        "code": ""}
            return {"outcome": state["phase"], "decl_no": state.get("decl_no") or "",
                    "prevented_duplicate": True, "reason": state.get("note") or "", "code": ""}
        # double check the customs system itself
        existing = system_status(self.client, decl)
        if existing and existing["status"] in ("approved", "submitted"):
            log_event("warn", "submit_prevented",
                      f"系统显示该单已是 {existing['status_text']}，跳过提交", decl_id=decl_id,
                      session_id=self.session_id, screenshot=self.client.screenshot("prevent_dup"))
            return {"outcome": existing["status"], "decl_no": existing["decl_no"],
                    "prevented_duplicate": True, "reason": "", "code": ""}
        owns = ensure_intent(decl, revision, str(decl.get("mock_decl_id") or ""))
        if not owns:
            log_event("warn", "submit_intent_owned",
                      "提交意图已存在，本进程改为先向系统核实",
                      decl_id=decl_id, session_id=self.session_id)
            existing = system_status(self.client, decl)
            if existing and existing["status"] in ("approved", "submitted", "rejected"):
                return {"outcome": existing["status"], "decl_no": existing["decl_no"],
                        "prevented_duplicate": True, "reason": "", "code": ""}
            # intent exists but the customs system has no submission: the previous
            # worker died between writing the intent and the click. Only take over
            # when that intent is stale, so a live worker is never raced.
            if not stale_intent(decl, revision, seconds=90):
                return {"outcome": "unknown", "decl_no": "",
                        "prevented_duplicate": True,
                        "reason": "另一进程持有提交意图且尚未出结果", "code": ""}
            log_event("warn", "submit_intent_takeover",
                      "遗留的提交意图已超时且系统无记录，接管并提交", decl_id=decl_id,
                      session_id=self.session_id)
            clear_intent(decl, revision)
            if not ensure_intent(decl, revision, str(decl.get("mock_decl_id") or "")):
                return {"outcome": "unknown", "decl_no": "", "prevented_duplicate": True,
                        "reason": "接管提交意图失败", "code": ""}
        log_event("info", "submit_intent", "已登记提交意图，准备提交", decl_id=decl_id,
                  session_id=self.session_id)
        result = self.client.submit(decl)
        self._refresh_session()
        conn = connect()
        try:
            if result.get("prevented_duplicate"):
                add_to_daily(conn, "duplicate_blocked", 1)
            else:
                add_to_daily(conn, "submitted_count", 1)
            conn.commit()
        finally:
            conn.close()
        phase = {"approved": "approved", "rejected": "rejected",
                 "duplicate_blocked": "approved"}.get(result.get("outcome"), "submitted")
        ledger_finish(decl, revision, phase=phase, decl_no=result.get("decl_no", ""),
                      outcome=result.get("outcome", ""), note=result.get("reason", ""))
        if result.get("outcome") == "unknown":
            log_event("error", "submit_unknown",
                      "提交结果页面未识别，将在回读阶段以系统状态为准", decl_id=decl_id,
                      session_id=self.session_id)
        return result

    def _revision_of(self, decl: dict[str, Any]) -> int:
        conn = connect()
        try:
            row = conn.execute("SELECT reject_attempts FROM declarations WHERE id=?",
                               (decl["id"],)).fetchone()
            return int(row["reject_attempts"] or 0) if row else 0
        finally:
            conn.close()

    # -- post submit ------------------------------------------------------
    def _after_submit(self, decl: dict[str, Any], submit: dict[str, Any]) -> dict[str, Any]:
        decl_id = decl["id"]
        overview = self._read_system(decl)
        status = overview["status"]
        if status == "approved":
            self._on_approved(decl, overview)
            return {"ok": True, "status": "approved", "decl_no": overview["decl_no"]}
        if status == "rejected":
            self._on_rejected(decl, overview)
            return {"ok": True, "status": "rejected", "decl_no": overview["decl_no"],
                    "reason": overview.get("reject_reason")}
        if status == "submitted":
            set_decl_status(decl_id, "submitted", submitted_at=now_iso())
            upsert_job(decl_id, {"status": "done", "finished_at": now_iso(), "step": "submitted"})
            log_event("warn", "pending_review", "已提交，等待审核结果", decl_id=decl_id,
                      session_id=self.session_id)
            return {"ok": True, "status": "submitted", "decl_no": overview["decl_no"]}
        self._fail(decl_id, "status_unknown", f"回读状态异常：{status}")
        return {"ok": False, "error": f"回读状态异常：{status}"}

    def _finalise_existing(self, decl: dict[str, Any], opened: dict[str, Any]) -> dict[str, Any]:
        overview = self._read_system(decl)
        if overview["status"] == "approved":
            self._on_approved(decl, overview)
        elif overview["status"] == "rejected":
            self._on_rejected(decl, overview)
        return {"ok": True, "skipped_submit": True, "status": overview["status"]}

    def _read_system(self, decl: dict[str, Any]) -> dict[str, Any]:
        rb = self.client.read_back(str(decl["mock_decl_id"]))
        status = {"审核通过": "approved", "已退单": "rejected", "已申报": "submitted",
                  "草稿": "draft"}.get(rb["status_text"], "unknown")
        raw_reason = ""
        if status == "rejected":
            # reject reason lives in the alert box on the detail page
            try:
                raw_reason = self.client.page.locator(".alert-error").first.inner_text().strip()
                raw_reason = raw_reason.replace("退单原因：", "").strip()
            except Exception:
                raw_reason = ""
        return {"status": status, "decl_no": rb["decl_no"], "header": rb["header"],
                "items": rb["items"], "reject_reason": raw_reason, "raw": rb}

    def _on_approved(self, decl: dict[str, Any], overview: dict[str, Any]) -> None:
        decl_id = decl["id"]
        rows = self._reconcile(decl, overview)
        mismatched = [r for r in rows if not r["match"]]
        conn = connect()
        try:
            existing = conn.execute("SELECT approved_at FROM declarations WHERE id=?",
                                    (decl_id,)).fetchone()
            add_to_daily(conn, "approved_count", 1) if not (existing and existing["approved_at"]) else None
            conn.commit()
        finally:
            conn.close()
        set_decl_status(decl_id, "approved", approved_at=now_iso(),
                        mock_decl_id=str(decl["mock_decl_id"]))
        upsert_job(decl_id, {"status": "done", "finished_at": now_iso(), "step": "approved",
                             "progress": 100})
        log_event("info", "approved",
                  f"审核通过 {overview['decl_no']}；回读比对 {len(rows)} 个字段，"
                  f"不一致 {len(mismatched)} 个", decl_id=decl_id, session_id=self.session_id,
                  screenshot=self.client.screenshot(f"approved_{decl['invoice_no']}"),
                  detail={"mismatched": [r["field_path"] for r in mismatched]})

    def _reconcile(self, decl: dict[str, Any], overview: dict[str, Any]) -> list[dict[str, Any]]:
        source = reconcile.build_source_snapshot(decl)
        actual = {"header": dict(overview["header"]), "items": overview["items"]}
        source["header"]["trade_mode"] = decl.get("trade_mode", "")
        actual["header"]["trade_mode"] = decl.get("trade_mode", "")
        rows = reconcile.compare(source, actual)
        reconcile.save_comparison(decl["id"], str(decl["mock_decl_id"]), rows,
                                  {"source": source, "system": actual,
                                   "decl_no": overview["decl_no"]})
        return rows

    def _on_rejected(self, decl: dict[str, Any], overview: dict[str, Any]) -> None:
        decl_id = decl["id"]
        raw = overview.get("reject_reason") or "系统退单（未提供原因）"
        parsed = rejections.parse_reason(raw)
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO rejections(declaration_id,mock_decl_id,decl_no,raw_reason,category,"
                "field_path,item_seq,suggestion,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(declaration_id,raw_reason) DO UPDATE SET "
                "updated_at=excluded.updated_at",
                (decl_id, str(decl.get("mock_decl_id") or ""), overview.get("decl_no"),
                 raw, parsed["category"], parsed["field_path"], parsed["item_seq"],
                 parsed["suggestion"], "open", now_iso(), now_iso()))
            conn.execute(
                "UPDATE declarations SET status='rejected', rejected_at=?, reject_reason=?, "
                "reject_category=?, mock_decl_id=?, reject_attempts=reject_attempts+1, "
                "updated_at=? WHERE id=?",
                (now_iso(), raw, parsed["category"], str(decl.get("mock_decl_id") or ""),
                 now_iso(), decl_id))
            add_to_daily(conn, "rejected_count", 1)
            conn.commit()
        finally:
            conn.close()
        upsert_job(decl_id, {"status": "done", "finished_at": now_iso(), "step": "rejected"})
        log_event("warn", "rejected", f"退单：{parsed['category_label']} — {raw}",
                  decl_id=decl_id, session_id=self.session_id,
                  screenshot=self.client.screenshot(f"rejected_{decl['invoice_no']}"),
                  detail=parsed)

    # -- helpers ----------------------------------------------------------
    def _refresh_session(self) -> None:
        expiry = self.client.current_session_expiry()
        delta = max(0, self.client.renewals - self._renewals_seen)
        self._renewals_seen = self.client.renewals
        touch_session(self.session_id, expires_at=expiry, renewed=bool(delta),
                      status="active" if expiry else None)

    def _fail(self, decl_id: int, step: str, message: str, screenshot: str | None = None,
              detail: Any = None) -> None:
        job = get_job(decl_id) or {}
        attempt = int(job.get("attempt") or 1)
        max_attempts = int(job.get("max_attempts") or 3)
        terminal = attempt >= max_attempts
        upsert_job(decl_id, {"status": "failed" if terminal else "pending",
                             "step": step, "note": message,
                             "finished_at": now_iso() if terminal else None})
        set_decl_status(decl_id, "abnormal" if terminal else "validated")
        log_event("error" if terminal else "warn", "job_failed",
                  f"[{step}] {message}" + ("（已达重试上限，转异常）" if terminal else "（将自动重试）"),
                  decl_id=decl_id, session_id=self.session_id, screenshot=screenshot,
                  detail=detail)


# --------------------------------------------------------------------------
# daemon
# --------------------------------------------------------------------------
class WorkerDaemon:
    def __init__(self, base_url: str | None = None, username: str | None = None,
                 password: str | None = None, poll_seconds: float = 2.0,
                 headless: bool = True, max_jobs: int | None = None):
        ensure_dirs()
        conn = connect()
        try:
            seed_settings(conn)
            self.base_url = base_url or get_setting(conn, "mock_base_url", "http://127.0.0.1:8099")
            self.username = username or get_setting(conn, "mock_username", "demo")
            self.password = password or get_setting(conn, "mock_password", "demo123")
            conn.commit()
        finally:
            conn.close()
        self.poll_seconds = poll_seconds
        self.headless = headless
        self.max_jobs = max_jobs
        self.stop_event = threading.Event()
        self.processed = 0
        self.last_error: str | None = None

    def stop(self) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        released = recover_stale()
        session_id = open_session("后台录入进程")
        client = MockCustomsClient(self.base_url, self.username, self.password,
                                   headless=self.headless,
                                   log=lambda lvl, ev, msg: log_event(
                                       lvl, f"client_{ev}", str(msg), session_id=session_id))
        recorder = Recorder(client, session_id, self.stop_event)
        try:
            client.start()
            client.login()
            touch_session(session_id, client.current_session_expiry())
            log_event("info", "daemon_started",
                      f"录入进程启动（{self.base_url}）" +
                      (f"；恢复中断任务 {released} 票" if released else ""),
                      session_id=session_id)
            idle_rounds = 0
            while not self.stop_event.is_set():
                did = self.run_once(client, recorder, session_id)
                if did:
                    self.processed += 1
                    idle_rounds = 0
                    if self.max_jobs and self.processed >= self.max_jobs:
                        break
                    continue
                idle_rounds += 1
                # recover declarations orphaned by a crashed worker once idle
                if idle_rounds % 5 == 0:
                    recover_stale()
                if self.max_jobs and idle_rounds >= 4:
                    break
                self.stop_event.wait(self.poll_seconds)
        finally:
            close_session(session_id)
            client.stop()
            log_event("info", "daemon_stopped", "录入进程已停止", session_id=session_id)

    def run_once(self, client: MockCustomsClient, recorder: Recorder,
                 session_id: int) -> bool:
        decls = eligible_declarations(limit=5)
        if not decls:
            return False
        decl = decls[0]
        try:
            recorder.record(decl["id"])
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            log_event("error", "daemon_error", self.last_error, decl_id=decl["id"],
                      session_id=session_id)
        return True


def run_single(decl_id: int, headless: bool = True, base_url: str | None = None,
               username: str | None = None, password: str | None = None) -> dict[str, Any]:
    ensure_dirs()
    conn = connect()
    try:
        seed_settings(conn)
        base_url = base_url or get_setting(conn, "mock_base_url", "http://127.0.0.1:8099")
        username = username or get_setting(conn, "mock_username", "demo")
        password = password or get_setting(conn, "mock_password", "demo123")
        conn.commit()
    finally:
        conn.close()
    session_id = open_session(f"手动录入 #{decl_id}")
    client = MockCustomsClient(base_url, username, password, headless=headless,
                               log=lambda lvl, ev, msg: log_event(
                                   lvl, f"client_{ev}", str(msg), decl_id=decl_id,
                                   session_id=session_id))
    try:
        client.start()
        client.login()
        touch_session(session_id, client.current_session_expiry())
        recorder = Recorder(client, session_id)
        result = recorder.record(decl_id)
        return result
    finally:
        close_session(session_id)
        client.stop()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="报关单自动录入进程")
    parser.add_argument("--decl-id", type=int, help="只录入指定的报关单（调试用）")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--max-jobs", type=int, default=None)
    parser.add_argument("--poll", type=float, default=2.0)
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args(argv)
    headless = not args.headed
    from declarant.common import init_db
    init_db()
    if args.decl_id:
        result = run_single(args.decl_id, headless=headless, base_url=args.base_url)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    daemon = WorkerDaemon(poll_seconds=args.poll, headless=headless,
                          max_jobs=args.max_jobs, base_url=args.base_url)

    def _stop(_signum, _frame):
        daemon.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    daemon.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
