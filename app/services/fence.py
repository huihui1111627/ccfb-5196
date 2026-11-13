"""Exactly-once submission fence.

The one rule that matters to a customs broker: **never declare the same 票
twice**.  A crash mid-entry is fine; a second accepted declaration is not.

Design
------
`submissions` has one row per document (`doc_id UNIQUE`) and a small state
machine.  The runner may only issue the *submit* HTTP POST when, inside a single
`BEGIN IMMEDIATE` transaction, it observes state `none` (or `uncertain`) and
flips it to `submitting` with its own claimant token.  The flip and the POST are
separated by crash-possible code, so recovery is by *evidence*, never by retry:

  state=none        -> no submit has ever been attempted; safe to enter fully
  state=submitting  -> claimed by a claimant that is no longer alive -> the
                       submit may or may not have reached the server; DO NOT
                       retry.  Query the mock system for the external ref:
                         * accepted declaration present -> settle accepted
                         * explicit rejection present with no declaration ->
                           settle rejected (re-entry allowed after fix)
                         * neither -> mark `uncertain`; an operator must
                           resolve it.  We never auto-resubmit.
  state=uncertain   -> same as above; requires operator decision.
  state=accepted    -> done, never submit again.
  state=rejected    -> a rejection is a definitive "not accepted", so a fixed
                       revision (new fingerprint) may be submitted.  A *same*
                       fingerprint resubmission is only allowed while the number
                       of definitive rejections is below max_resubmit, and it
                       still goes through the same claim.

The fence deliberately does not trust a local "submitted" flag written after the
HTTP call: the flag write can be lost to a kill between POST and commit.  The
server-side query is the authority.
"""

from __future__ import annotations

import hashlib
import json
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db import Database, now_iso

ST_NONE = "none"
ST_SUBMITTING = "submitting"
ST_UNCERTAIN = "uncertain"
ST_ACCEPTED = "accepted"
ST_REJECTED = "rejected"

ACTIVE_STATES = (ST_SUBMITTING,)


class FenceError(Exception):
    pass


def claimant_id(prefix: str = "runner") -> str:
    return f"{prefix}:{socket.gethostname()}:{uuid.uuid4().hex[:12]}"


def payload_sha(header: dict, items: list[dict]) -> str:
    from common.formatting import canonical_for_hash

    payload = canonical_for_hash({
        "header": {k: v for k, v in header.items()
                   if k not in ("declaration_no", "external_ref")},
        "items": [{k: v for k, v in it.items() if k != "item_no"} for it in items],
    })
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _row(db: Database, doc_id: int):
    return db.one("SELECT * FROM submissions WHERE doc_id=?", (doc_id,))


def ensure_row(db: Database, doc_id: int, payload_sha_value: str) -> None:
    db.execute(
        "INSERT INTO submissions(doc_id, state, payload_sha, updated_at) "
        "VALUES(?,?,?,?) ON CONFLICT(doc_id) DO NOTHING",
        (doc_id, ST_NONE, payload_sha_value, now_iso()),
    )


def get_state(db: Database, doc_id: int) -> str:
    row = _row(db, doc_id)
    return row["state"] if row else ST_NONE


@dataclass
class Claim:
    doc_id: int
    attempt_seq: int
    claimant: str
    payload_sha: str
    previous_state: str


def acquire_claim(db: Database, doc_id: int, claimant: str, sha: str,
                  max_resubmit: int = 3) -> Claim:
    """Atomically claim the right to submit this document *once more*.

    Raises FenceError when the document must not be (re)submitted.
    """
    with db.tx(immediate=True):
        row = _row(db, doc_id)
        if row is None:
            db.execute(
                "INSERT INTO submissions(doc_id, state, payload_sha, updated_at) "
                "VALUES(?,?,?,?)", (doc_id, ST_NONE, sha, now_iso()),
            )
            row = _row(db, doc_id)
        state = row["state"]
        if state == ST_ACCEPTED:
            raise FenceError("该票已有受理记录，禁止再次申报")
        if state == ST_SUBMITTING:
            raise FenceError("该票正处于提交中，禁止重复认领")
        if state == ST_UNCERTAIN:
            raise FenceError("该票上次提交结果不明，需人工核实后才能重录")
        if state == ST_REJECTED:
            rejected_count = db.scalar(
                "SELECT COUNT(*) FROM submission_attempts WHERE doc_id=? AND kind='submit' "
                "AND outcome='rejected'", (doc_id,)) or 0
            if rejected_count >= max_resubmit:
                raise FenceError(f"该票已退单 {rejected_count} 次，超过重录上限，请人工处理")
        attempt_seq = (row["attempt_seq"] or 0) + 1
        db.execute(
            "UPDATE submissions SET state=?, claimant=?, attempt_seq=?, payload_sha=?, "
            "submitted_at=?, response_kind=NULL, last_error=NULL, updated_at=? WHERE doc_id=?",
            (ST_SUBMITTING, claimant, attempt_seq, sha, now_iso(), now_iso(), doc_id),
        )
        db.execute(
            "INSERT INTO submission_attempts(doc_id, seq, kind, claimant, outcome, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (doc_id, attempt_seq, "submit", claimant, "started", now_iso()),
        )
        return Claim(doc_id=doc_id, attempt_seq=attempt_seq, claimant=claimant,
                     payload_sha=sha, previous_state=state)


def record_draft(db: Database, doc_id: int, claimant: str) -> None:
    db.execute(
        "UPDATE submissions SET drafted_at=?, updated_at=? WHERE doc_id=?",
        (now_iso(), now_iso(), doc_id),
    )
    row = _row(db, doc_id)
    seq = row["attempt_seq"] if row and row["attempt_seq"] else 1
    db.execute(
        "INSERT OR IGNORE INTO submission_attempts(doc_id, seq, kind, claimant, outcome, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (doc_id, seq, "draft", claimant, "started", now_iso()),
    )


def settle_accepted(db: Database, doc_id: int, claimant: str, declaration_no: str,
                    detail: str = "") -> None:
    with db.tx(immediate=True):
        row = _row(db, doc_id)
        if row and row["state"] == ST_ACCEPTED:
            return
        db.execute(
            "UPDATE submissions SET state=?, response_kind='accepted', declaration_no=?, "
            "settled_at=?, claimant=COALESCE(claimant,?), updated_at=? WHERE doc_id=?",
            (ST_ACCEPTED, declaration_no, now_iso(), claimant, now_iso(), doc_id),
        )
        db.execute(
            "UPDATE submission_attempts SET outcome='accepted', declaration_no=? "
            "WHERE doc_id=? AND kind='submit' AND outcome='started' AND seq=?",
            (declaration_no, doc_id, (row["attempt_seq"] if row else 1)),
        )
        db.execute(
            "INSERT OR IGNORE INTO submission_attempts(doc_id, seq, kind, claimant, outcome, "
            "declaration_no, detail, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, (row["attempt_seq"] if row else 1), "settle", claimant or "system",
             "accepted", declaration_no, detail, now_iso()),
        )


def settle_rejected(db: Database, doc_id: int, claimant: str, reason_code: str,
                    reason_text: str, category: str = "", hint: str = "",
                    detail: str = "") -> None:
    with db.tx(immediate=True):
        row = _row(db, doc_id)
        db.execute(
            "UPDATE submissions SET state=?, response_kind='rejected', last_error=?, "
            "settled_at=?, claimant=COALESCE(claimant,?), updated_at=? WHERE doc_id=?",
            (ST_REJECTED, f"{reason_code}:{reason_text}"[:500], now_iso(), claimant,
             now_iso(), doc_id),
        )
        seq = row["attempt_seq"] if row else 1
        db.execute(
            "UPDATE submission_attempts SET outcome='rejected', detail=? "
            "WHERE doc_id=? AND kind='submit' AND outcome='started' AND seq=?",
            (f"{reason_code}:{reason_text}"[:500], doc_id, seq),
        )
        db.execute(
            "INSERT OR IGNORE INTO rejections(doc_id, reason_code, reason_text, category, "
            "hint, source, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,'mock','open',?,?) "
            "ON CONFLICT(doc_id) DO UPDATE SET reason_code=excluded.reason_code, "
            "reason_text=excluded.reason_text, category=excluded.category, "
            "hint=excluded.hint, status='open', updated_at=excluded.updated_at",
            (doc_id, reason_code, reason_text, category, hint, now_iso(), now_iso()),
        )


def settle_uncertain(db: Database, doc_id: int, claimant: str, detail: str) -> None:
    with db.tx(immediate=True):
        row = _row(db, doc_id)
        if row and row["state"] == ST_ACCEPTED:
            return
        db.execute(
            "UPDATE submissions SET state=?, response_kind='unknown', last_error=?, "
            "updated_at=? WHERE doc_id=?",
            (ST_UNCERTAIN, detail[:500], now_iso(), doc_id),
        )
        seq = row["attempt_seq"] if row else 1
        db.execute(
            "UPDATE submission_attempts SET outcome='uncertain', detail=? "
            "WHERE doc_id=? AND kind='submit' AND outcome='started' AND seq=?",
            (detail[:500], doc_id, seq),
        )


def release_unsubmitted(db: Database, doc_id: int, claimant: str, reason: str) -> None:
    """Release a claim when the server explicitly confirms nothing was submitted.

    This must only be called with *positive* evidence that no POST reached the
    server (e.g. it bounced to the login page).  It resets the fence to the state
    it was in before the claim so a later run can enter the ticket normally.
    """
    with db.tx(immediate=True):
        row = _row(db, doc_id)
        if not row:
            return
        if row["state"] == ST_ACCEPTED:
            raise FenceError("已有受理记录，不能作为未提交释放")
        previous = ST_REJECTED if row["response_kind"] == "rejected" else ST_NONE
        db.execute(
            "UPDATE submissions SET state=?, claimant=NULL, response_kind=NULL, "
            "last_error=?, updated_at=? WHERE doc_id=?",
            (previous, f"未提交释放:{reason}"[:500], now_iso(), doc_id),
        )
        db.execute(
            "UPDATE submission_attempts SET outcome='not_sent', detail=? "
            "WHERE doc_id=? AND kind='submit' AND outcome='started' AND seq=?",
            (reason[:500], doc_id, row["attempt_seq"] or 1),
        )


def operator_resolve(db: Database, doc_id: int, resolution: str, note: str,
                     declaration_no: str = "", operator: str = "operator") -> str:
    """Human decision for an `uncertain` document.

    resolution: "accepted" (system shows it) or "not_submitted" (proven absent).
    """
    row = _row(db, doc_id)
    if not row:
        raise FenceError("无提交记录")
    if resolution == "accepted":
        settle_accepted(db, doc_id, operator, declaration_no, detail=note)
        return ST_ACCEPTED
    if resolution == "not_submitted":
        if row["state"] == ST_ACCEPTED:
            raise FenceError("已有受理记录，不能标记为未提交")
        with db.tx(immediate=True):
            db.execute(
                "UPDATE submissions SET state=?, response_kind=NULL, last_error=?, "
                "updated_at=? WHERE doc_id=?",
                (ST_REJECTED if row["state"] == ST_REJECTED else ST_NONE,
                 f"operator:{note}"[:500], now_iso(), doc_id),
            )
            db.execute(
                "INSERT OR IGNORE INTO submission_attempts(doc_id, seq, kind, claimant, outcome, "
                "detail, created_at) VALUES(?,?,?,?,?,?,?)",
                (doc_id, (row["attempt_seq"] or 1), "resolve", operator,
                 "not_submitted", note[:500], now_iso()),
            )
            if row["state"] == ST_REJECTED:
                db.execute("UPDATE submissions SET state=? WHERE doc_id=?", (ST_REJECTED, doc_id))
        return "released"
    raise FenceError(f"未知处理方式：{resolution}")


def is_duplicate_risk(db: Database, doc_id: int) -> bool:
    state = get_state(db, doc_id)
    return state in (ST_SUBMITTING, ST_UNCERTAIN, ST_ACCEPTED)


def stale_claims(db: Database, older_than_seconds: int = 0) -> list:
    """Rows stuck in `submitting` whose claimant is dead (heartbeat stale)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
              ).isoformat(timespec="seconds")
    return db.query(
        "SELECT s.*, d.external_ref FROM submissions s JOIN documents d ON d.id=s.doc_id "
        "WHERE s.state='submitting' AND s.updated_at <= ?", (cutoff,),
    )
