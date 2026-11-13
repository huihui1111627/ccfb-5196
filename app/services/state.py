"""Document lifecycle rules.

States
------
pending     imported, not yet validated
needs_fix   blocking validation errors
ready       validated, eligible for entry
queued      picked for a run
submitting  a claimant is actively entering it
submitted   accepted by customs (declaration_no known)
rejected    退回数; needs operator handling before re-entry
revision     operator edited after rejection; ready to re-enter
verified            read-back matched source
verified_mismatch   read-back found field differences
failed      exhausted retries / unrecoverable
cancelled   operator removed

Legal transitions are enforced centrally so no route can accidentally move a
document into a state that would allow a duplicate submission.
"""

from __future__ import annotations

from app.db import Database, now_iso

TRANSITIONS = {
    "pending": {"ready", "needs_fix", "queued", "cancelled"},
    "needs_fix": {"ready", "pending", "cancelled", "revision", "queued", "rejected"},
    "ready": {"queued", "needs_fix", "cancelled", "revision"},
    "queued": {"ready", "submitting", "submitted", "needs_fix", "cancelled", "failed"},
    "submitting": {"submitted", "rejected", "ready", "failed", "cancelled", "queued"},
    "submitted": {"verified", "verified_mismatch", "cancelled"},
    "rejected": {"revision", "cancelled", "failed", "queued"},
    "revision": {"ready", "queued", "needs_fix", "cancelled"},
    "verified": {"verified_mismatch", "cancelled"},
    "verified_mismatch": {"verified", "cancelled", "revision"},
    "failed": {"revision", "queued", "cancelled", "submitted", "verified",
               "verified_mismatch", "rejected"},
    "cancelled": {"revision", "ready"},
}

# Statuses whose documents may be put into an entry run.
ENTRY_ELIGIBLE = ("ready", "queued", "revision", "failed", "submitting")


class IllegalTransition(Exception):
    pass


def set_document_state(db: Database, doc_id: int, new_state: str,
                       reason: str = "", allow_same: bool = True) -> bool:
    doc = db.one("SELECT status, external_ref FROM documents WHERE id=?", (doc_id,))
    if not doc:
        return False
    old = doc["status"]
    if old == new_state:
        if allow_same:
            db.execute("UPDATE documents SET updated_at=? WHERE id=?", (now_iso(), doc_id))
        return allow_same
    if new_state not in TRANSITIONS.get(old, set()):
        raise IllegalTransition(
            f"{doc['external_ref']}: 状态 {old} -> {new_state} 不允许"
        )
    db.execute("UPDATE documents SET status=?, updated_at=? WHERE id=?",
               (new_state, now_iso(), doc_id))
    log_event(db, doc_id, "state", new_state, reason)
    return True


def _system_run(db: Database) -> int:
    """Non-runner events (imports, operator edits) attach to a singleton run."""
    row = db.one("SELECT id FROM runs WHERE mode='system' ORDER BY id LIMIT 1")
    if row:
        return row["id"]
    return db.execute(
        "INSERT INTO runs(started_at, status, mode, claimant, heartbeat_at, stats_json) "
        "VALUES(?,?,?,?,?,?)",
        (now_iso(), "finished", "system", "system", now_iso(), "{}"),
    )


def log_event(db: Database, doc_id: int | None, kind: str, status: str,
              message: str = "", run_id: int | None = None,
              item_no: int | None = None, step: str | None = None,
              screenshot_path: str | None = None, url: str | None = None) -> None:
    run_id = run_id if run_id is not None else _system_run(db)
    db.execute(
        "INSERT INTO run_steps(run_id, doc_id, item_no, step, status, message, "
        "screenshot_path, url, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (run_id, doc_id, item_no, step or kind, status, message,
         screenshot_path, url, now_iso()),
    )


def status_counts(db: Database) -> dict:
    rows = db.query("SELECT status, COUNT(*) n FROM documents GROUP BY status")
    counts = {r["status"]: r["n"] for r in rows}
    total = sum(counts.values())
    counts["total"] = total
    counts["needs_action"] = sum(
        counts.get(s, 0) for s in
        ("needs_fix", "ready", "queued", "rejected", "revision", "verified_mismatch", "failed")
    )
    return counts


def next_entry_document(db: Database):
    """Lowest-sequence document that still needs to be entered."""
    return db.one(
        "SELECT * FROM documents WHERE status IN ('ready','queued','revision','failed','submitting') "
        "ORDER BY sequence_no, id LIMIT 1"
    )


def queue_documents(db: Database, doc_ids: list[int] | None = None,
                    batch_id: int | None = None, include_needs_fix: bool = False) -> dict:
    sql = ("SELECT id, status FROM documents WHERE status IN ('ready','revision','failed') "
           + ("OR status='needs_fix' " if include_needs_fix else ""))
    params: list = []
    if doc_ids:
        placeholders = ",".join("?" for _ in doc_ids)
        sql = ("SELECT id, status FROM documents WHERE id IN (%s) AND (status IN "
               "('ready','revision','failed')%s)" % (
                   placeholders, " OR status='needs_fix'" if include_needs_fix else ""))
        params = list(doc_ids)
    elif batch_id:
        sql += " AND batch_id=?"
        params.append(batch_id)
    docs = db.query(sql, tuple(params))
    queued = []
    for d in docs:
        if d["status"] in ("ready", "revision", "failed"):
            set_document_state(db, d["id"], "queued", "加入待录队列")
        queued.append(d["id"])
    return {"queued": len(queued), "document_ids": queued}
