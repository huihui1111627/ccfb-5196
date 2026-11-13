"""The exactly-once fence: claim/state machine and crash semantics."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Database
from app.services import fence
from app.services.state import IllegalTransition, set_document_state


def _make_doc(db: Database, ref: str = "INV-TEST-1", status: str = "ready") -> int:
    return db.execute(
        "INSERT INTO documents(batch_id, external_ref, source_kind, source_file, page_ref, "
        "sequence_no, status, header_json, items_json, validation_json, validation_blocking, "
        "fingerprint, created_at, updated_at) VALUES(NULL,?,?,?,?,?,?,?,?,'{}',0,?,?,?)",
        (ref, "excel", "f.xlsx", "p1", 0, status,
         json.dumps({"external_ref": ref, "trade_mode": "0100"}),
         json.dumps([{"item_no": 1, "hs_code": "8471301000"}]),
         "fp-" + ref, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    db.conn().commit()


SHA = "a" * 64


def test_claim_then_settle_accepted(db):
    doc = _make_doc(db)
    claim = fence.acquire_claim(db, doc, "c1", SHA)
    assert claim.attempt_seq == 1
    assert fence.get_state(db, doc) == fence.ST_SUBMITTING
    fence.settle_accepted(db, doc, "c1", "DECL-1")
    assert fence.get_state(db, doc) == fence.ST_ACCEPTED
    row = db.one("SELECT * FROM submissions WHERE doc_id=?", (doc,))
    assert row["declaration_no"] == "DECL-1"
    # second claim must fail
    with pytest.raises(fence.FenceError):
        fence.acquire_claim(db, doc, "c2", SHA)


def test_claim_blocked_while_submitting(db):
    doc = _make_doc(db)
    fence.acquire_claim(db, doc, "c1", SHA)
    with pytest.raises(fence.FenceError):
        fence.acquire_claim(db, doc, "c2", SHA)


def test_uncertain_blocks_resubmission(db):
    doc = _make_doc(db)
    fence.acquire_claim(db, doc, "c1", SHA)
    fence.settle_uncertain(db, doc, "c1", "crash")
    with pytest.raises(fence.FenceError):
        fence.acquire_claim(db, doc, "c2", SHA)


def test_rejection_allows_one_reentry_then_blocks_on_uncertain(db):
    doc = _make_doc(db)
    fence.acquire_claim(db, doc, "c1", SHA)
    fence.settle_rejected(db, doc, "c1", "R01", "要素缺失", category="element")
    assert fence.get_state(db, doc) == fence.ST_REJECTED
    claim2 = fence.acquire_claim(db, doc, "c2", SHA)
    assert claim2.attempt_seq == 2
    # after re-submit the fence is submitting again
    with pytest.raises(fence.FenceError):
        fence.acquire_claim(db, doc, "c3", SHA)


def test_max_resubmit_is_enforced(db):
    doc = _make_doc(db)
    for i in range(3):
        fence.acquire_claim(db, doc, f"c{i}", SHA, max_resubmit=3)
        fence.settle_rejected(db, doc, f"c{i}", "R01", "退回")
    with pytest.raises(fence.FenceError) as exc:
        fence.acquire_claim(db, doc, "c9", SHA, max_resubmit=3)
    assert "超过重录上限" in str(exc.value)


def test_operator_resolve_accepted_and_not_submitted(db):
    doc = _make_doc(db)
    fence.acquire_claim(db, doc, "c1", SHA)
    fence.settle_uncertain(db, doc, "c1", "crash")
    fence.operator_resolve(db, doc, "accepted", "人工确认", "DECL-9")
    assert fence.get_state(db, doc) == fence.ST_ACCEPTED
    with pytest.raises(fence.FenceError):
        fence.operator_resolve(db, doc, "not_submitted", "想撤回", "")
    # A fresh doc can be released.
    doc2 = _make_doc(db, "INV-TEST-2")
    fence.acquire_claim(db, doc2, "c1", SHA)
    fence.settle_uncertain(db, doc2, "c1", "crash")
    assert fence.operator_resolve(db, doc2, "not_submitted", "确认无记录", "") == "released"
    assert fence.get_state(db, doc2) == fence.ST_NONE
    fence.acquire_claim(db, doc2, "c2", SHA)


def test_concurrent_claims_exactly_one_wins(db, broker_env):
    doc = _make_doc(db, "INV-RACE")
    # Close the fixture's thread-local connection so worker threads see a
    # fully-committed WAL snapshot (no open read txn from this thread).
    db.conn().close()
    results: list[str] = []
    barrier = threading.Barrier(6)

    def worker(n: int):
        local = Database()
        barrier.wait()
        try:
            fence.acquire_claim(local, doc, f"w{n}", SHA)
            results.append("claimed")
        except fence.FenceError:
            results.append("blocked")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("claimed") == 1, results
    assert results.count("blocked") == 5
    attempts = db.query(
        "SELECT * FROM submission_attempts WHERE doc_id=? AND kind='submit'", (doc,))
    assert len(attempts) == 1


def test_state_machine_rejects_illegal_moves(db):
    doc = _make_doc(db, "INV-SM", status="submitted")
    with pytest.raises(IllegalTransition):
        set_document_state(db, doc, "queued")
    set_document_state(db, doc, "verified")
    assert db.one("SELECT status FROM documents WHERE id=?", (doc,))["status"] == "verified"


def test_submission_row_is_unique_per_document(db):
    doc = _make_doc(db, "INV-UQ")
    fence.ensure_row(db, doc, SHA)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO submissions(doc_id, state, payload_sha, updated_at) VALUES(?,?,?,?)",
            (doc, "none", SHA, "2026-01-01T00:00:00+00:00"))


def test_crash_between_claim_and_settle_is_visible(db):
    """A claim committed but never settled must be found by stale_claims."""
    doc = _make_doc(db, "INV-CRASH")
    fence.acquire_claim(db, doc, "dead-claimant", SHA)
    stale = fence.stale_claims(db, older_than_seconds=-1)
    assert any(r["doc_id"] == doc for r in stale)
    assert fence.is_duplicate_risk(db, doc)


def test_release_unsubmitted_returns_to_none(db):
    doc = _make_doc(db, "INV-RELEASE")
    fence.acquire_claim(db, doc, "c1", SHA)
    fence.release_unsubmitted(db, doc, "c1", "会话过期，请求未发出")
    assert fence.get_state(db, doc) == fence.ST_NONE
    claim = fence.acquire_claim(db, doc, "c2", SHA)
    assert claim.attempt_seq == 2
    att = db.query(
        "SELECT outcome FROM submission_attempts WHERE doc_id=? AND kind='submit' "
        "ORDER BY id", (doc,))
    assert [a["outcome"] for a in att] == ["not_sent", "started"]


def test_release_never_overrides_accepted(db):
    doc = _make_doc(db, "INV-ACCEPTED")
    fence.acquire_claim(db, doc, "c1", SHA)
    fence.settle_accepted(db, doc, "c1", "DECL-1")
    with pytest.raises(fence.FenceError):
        fence.release_unsubmitted(db, doc, "c1", "oops")
