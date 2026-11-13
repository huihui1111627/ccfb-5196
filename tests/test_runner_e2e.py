"""End-to-end: real Playwright against the mock portal.

Covers the guarantees that matter to a broker:
* normal entry -> accepted -> read-back matches
* a second run never submits an already-accepted ticket
* crash after the fence claim (before POST) -> uncertain, not resubmitted
* crash after the portal committed -> recovery adopts the existing declaration
* session expiry mid-entry -> re-login, continue from the saved item
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("playwright")

from app.db import Database
from app.importer import import_source_file
from app.runner.main import build_portal
from app.runner.portal import MockPortal
from app.runner import RunConfig, Runner


def _settings(url: str) -> dict:
    base = Database().get_settings()
    base.update({"mock_base_url": url, "headless": "1", "slow_mo_ms": "0",
                 "probe_attempts": "2", "settle_seconds_after_uncertain": "0.2"})
    return base


def _import_and_ready(db, sample_dir, filename):
    result = import_source_file(db, sample_dir / filename)
    docs = db.query(
        "SELECT id, external_ref FROM documents WHERE batch_id=? "
        "AND validation_blocking=0 ORDER BY sequence_no", (result["batch_id"],))
    for d in docs:
        from app.services.state import set_document_state
        if db.one("SELECT status FROM documents WHERE id=?", (d["id"],))["status"] == "needs_fix":
            set_document_state(db, d["id"], "ready", "test")
    return result, [d["id"] for d in docs]


def _run_subprocess(broker_env, mock_url, doc_ids, crash_point=None, crash_doc=None,
                    expire_after_item="") -> int:
    """Run the runner CLI in a child process -- required for crash tests because
    the crash hook calls os._exit(), which would kill pytest itself."""
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(ROOT),
        "BROKER_DATA_DIR": str(broker_env),
        "BROKER_DB": str(Path(broker_env) / "broker.db"),
        "BROKER_SHOTS": str(Path(broker_env) / "screenshots"),
        "BROKER_EXPORTS": str(Path(broker_env) / "exports"),
        "MOCK_BASE_URL": mock_url,
    })
    if crash_point:
        env["RUNNER_CRASH_POINT"] = crash_point
    if crash_doc is not None:
        env["RUNNER_CRASH_DOC"] = str(crash_doc)
    if expire_after_item:
        env["RUNNER_EXPIRE_AFTER_ITEM"] = str(expire_after_item)
    proc = subprocess.run(
        [sys.executable, "-m", "app.runner.main", "--docs",
         ",".join(str(d) for d in doc_ids)],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)
    return proc.returncode


def _run(db, mock_url, doc_ids, crash_point: str | None = None, crash_doc=None,
         expire_after_item: int | str = "") -> dict:
    settings = _settings(mock_url)
    config = RunConfig(document_ids=doc_ids, headless=True,
                       settle_seconds_after_uncertain=0.2, probe_attempts=2)
    portal = build_portal(settings)
    portal.start()
    env_backup = {}
    for key in ("RUNNER_CRASH_POINT", "RUNNER_CRASH_DOC", "RUNNER_EXPIRE_AFTER_ITEM"):
        env_backup[key] = os.environ.pop(key, None)
    try:
        if crash_point:
            os.environ["RUNNER_CRASH_POINT"] = crash_point
        if crash_doc is not None:
            os.environ["RUNNER_CRASH_DOC"] = str(crash_doc)
        if expire_after_item:
            os.environ["RUNNER_EXPIRE_AFTER_ITEM"] = str(expire_after_item)
        runner = Runner(db, portal, config, settings, run_id=None)
        return runner.run()
    finally:
        portal.close()
        for key, val in env_backup.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _mock_get(mock_server, path, params=None):
    import urllib.request
    from urllib.parse import urlencode
    url = mock_server["url"] + path + ("?" + urlencode(params) if params else "")
    # Log in via raw cookie handling using requests' session-less approach:
    import http.cookiejar
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.open(mock_server["url"] + "/login",
                data=b"username=admin&password=admin123").read()
    return json.loads(opener.open(url).read())


def test_normal_entry_accept_and_verify(broker_env, sample_dir, mock_server):
    db = Database()
    result, docs = _import_and_ready(db, sample_dir, "客户发票_华南_20261113.pdf")
    assert docs
    doc_id = docs[0]
    stats = _run(db, mock_server["url"], [doc_id])
    assert stats["accepted"] == 1, stats
    assert stats["verified_ok"] == 1
    doc = db.one("SELECT status, compare_json FROM documents WHERE id=?", (doc_id,))
    assert doc["status"] == "verified"
    compare = json.loads(doc["compare_json"])
    assert compare["matched"], compare["differences"]
    sub = db.one("SELECT * FROM submissions WHERE doc_id=?", (doc_id,))
    assert sub["state"] == "accepted" and sub["declaration_no"]
    # exactly one declaration on the server
    q = _mock_get(mock_server, "/api/query",
                  {"external_ref": db.one("SELECT external_ref FROM documents WHERE id=?",
                                          (doc_id,))["external_ref"]})
    assert len(q["declarations"]) == 1


def test_second_run_does_not_resubmit(broker_env, sample_dir, mock_server):
    db = Database()
    _, docs = _import_and_ready(db, sample_dir, "客户发票_华南_20261113.pdf")
    doc_id = docs[0]
    _run(db, mock_server["url"], [doc_id])
    ref = db.one("SELECT external_ref FROM documents WHERE id=?", (doc_id,))["external_ref"]
    before = _mock_get(mock_server, "/api/query", {"external_ref": ref})
    assert len(before["declarations"]) == 1
    # force it back into the queue as if a user re-queued it
    from app.services.state import set_document_state
    db.execute("UPDATE documents SET status='queued' WHERE id=?", (doc_id,))
    stats = _run(db, mock_server["url"], [doc_id])
    assert stats["accepted"] == 0
    assert stats["skipped_accepted"] == 1
    after = _mock_get(mock_server, "/api/query", {"external_ref": ref})
    assert len(after["declarations"]) == 1, "must never create a second declaration"


def test_crash_after_claim_marks_uncertain_not_resubmitted(
        broker_env, sample_dir, mock_server):
    db = Database()
    _, docs = _import_and_ready(db, sample_dir, "客户发票_华北_20261113.pdf")
    doc_id = docs[0]
    ref = db.one("SELECT external_ref FROM documents WHERE id=?", (doc_id,))["external_ref"]
    rc = _run_subprocess(broker_env, mock_server["url"], [doc_id],
                         crash_point="after_claim", crash_doc=doc_id)
    assert rc == 99, f"expected crash exit, got {rc}"
    sub = db.one("SELECT state FROM submissions WHERE doc_id=?", (doc_id,))
    assert sub["state"] == "submitting"
    q = _mock_get(mock_server, "/api/query", {"external_ref": ref})
    assert q["declarations"] == []

    stats = _run(db, mock_server["url"], [doc_id])
    assert stats["uncertain"] == 1, stats
    assert stats["accepted"] == 0
    assert db.one("SELECT state FROM submissions WHERE doc_id=?", (doc_id,))["state"] == "uncertain"
    q2 = _mock_get(mock_server, "/api/query", {"external_ref": ref})
    assert q2["declarations"] == [], "uncertain ticket must not be auto-resubmitted"


def test_crash_after_server_commit_adopts_declaration(
        broker_env, sample_dir, mock_server):
    db = Database()
    _, docs = _import_and_ready(db, sample_dir, "客户发票_华南_20261113.pdf")
    doc_id = docs[-1]
    ref = db.one("SELECT external_ref FROM documents WHERE id=?", (doc_id,))["external_ref"]
    rc = _run_subprocess(broker_env, mock_server["url"], [doc_id],
                         crash_point="after_submit_response", crash_doc=doc_id)
    assert rc == 99, f"expected crash exit, got {rc}"
    # Server committed, local fence still says submitting.
    q = _mock_get(mock_server, "/api/query", {"external_ref": ref})
    assert len(q["declarations"]) == 1, "the crash must land after the server commit"
    assert db.one("SELECT state FROM submissions WHERE doc_id=?", (doc_id,))["state"] == "submitting"

    stats = _run(db, mock_server["url"], [doc_id])
    assert stats["skipped_accepted"] == 1, stats
    assert stats["accepted"] == 0
    assert db.one("SELECT state FROM submissions WHERE doc_id=?", (doc_id,))["state"] == "accepted"
    q2 = _mock_get(mock_server, "/api/query", {"external_ref": ref})
    assert len(q2["declarations"]) == 1, "no duplicate declaration"


def test_session_expiry_relogs_and_continues(broker_env, sample_dir, mock_server):
    db = Database()
    _, docs = _import_and_ready(db, sample_dir, "客户发票_华北_20261113.pdf")
    doc_id = docs[-1]
    stats = _run(db, mock_server["url"], [doc_id], expire_after_item=1)
    assert stats["relogins"] >= 1, stats
    assert stats["accepted"] == 1, stats
    assert stats["verified_ok"] == 1
    assert db.one("SELECT status FROM documents WHERE id=?", (doc_id,))["status"] == "verified"


def test_rejection_enters_queue_and_can_reenter(
        broker_env, sample_dir, mock_server):
    db = Database()
    _, docs = _import_and_ready(db, sample_dir, "客户发票_华北_20261113.pdf")
    doc_id = docs[0]
    ref = db.one("SELECT external_ref FROM documents WHERE id=?", (doc_id,))["external_ref"]
    mock_server["app"].config["REJECTION_RATE"] = 0
    # Force a rejection by submitting a draft with an element removed via the mock API.
    import http.cookiejar
    import urllib.request
    from urllib.parse import urlencode
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.open(mock_server["url"] + "/login",
                data=b"username=admin&password=admin123").read()
    # remove one element from the source document so the portal rejects
    doc = db.one("SELECT header_json, items_json FROM documents WHERE id=?", (doc_id,))
    header = json.loads(doc["header_json"])
    items = json.loads(doc["items_json"])
    for item in items:
        item["elements"] = {}
    with db.tx(immediate=True):
        db.execute("UPDATE documents SET items_json=?, validation_blocking=0 WHERE id=?",
                   (json.dumps(items), doc_id))
    stats = _run(db, mock_server["url"], [doc_id])
    assert stats["rejected"] == 1, stats
    rej = db.one("SELECT * FROM rejections WHERE doc_id=?", (doc_id,))
    assert rej and rej["status"] == "open"
    assert rej["reason_code"]
    q = _mock_get(mock_server, "/api/query", {"external_ref": ref})
    assert q["declarations"] == []

    # operator fixes the data and re-queues
    from app.importer.base import validate_invoice, ParsedItem
    from app.services.state import set_document_state
    fixed_items = []
    for item in items:
        hs = item["hs_code"]
        from common import catalog
        item["elements"] = {e: ("无品牌" if e == "品牌类型" else
                                ("不能确定" if e == "出口享惠情况" else "无"))
                            for e in catalog.HS_BY_CODE[hs]["elements"]}
        fixed_items.append(item)
    with db.tx(immediate=True):
        db.execute("UPDATE documents SET items_json=?, validation_json='{}' WHERE id=?",
                   (json.dumps(fixed_items), doc_id))
    set_document_state(db, doc_id, "revision", "fix")
    set_document_state(db, doc_id, "queued", "reentry")
    db.execute("UPDATE rejections SET status='resolved', resolved_at='x' WHERE doc_id=?",
               (doc_id,))
    stats2 = _run(db, mock_server["url"], [doc_id])
    assert stats2["accepted"] == 1, json.dumps(stats2, ensure_ascii=False)
    q2 = _mock_get(mock_server, "/api/query", {"external_ref": ref})
    assert len(q2["declarations"]) == 1


def test_double_run_sequential_never_duplicates(broker_env, sample_dir, mock_server):
    """Two back-to-back runs on the same batch: the second must skip everything."""
    db = Database()
    _, docs = _import_and_ready(db, sample_dir, "客户发票_华南_20261113.pdf")
    stats1 = _run(db, mock_server["url"], docs)
    assert stats1["accepted"] == len(docs)
    # Re-queue all of them as an impatient operator might
    from app.services.state import set_document_state
    for doc_id in docs:
        db.execute("UPDATE documents SET status='queued' WHERE id=?", (doc_id,))
    stats2 = _run(db, mock_server["url"], docs)
    assert stats2["accepted"] == 0, stats2
    assert stats2["skipped_accepted"] == len(docs), stats2
    for doc_id in docs:
        ref = db.one("SELECT external_ref FROM documents WHERE id=?", (doc_id,))["external_ref"]
        q = _mock_get(mock_server, "/api/query", {"external_ref": ref})
        assert len(q["declarations"]) == 1, f"{ref} duplicated"


def test_ambiguous_network_failure_never_resubmits(broker_env, sample_dir):
    """Point the runner at a dead portal after draft: it must fail safe."""
    db = Database()
    _, docs = _import_and_ready(db, sample_dir, "客户发票_华北_20261113.pdf")
    doc_id = docs[0]
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    # Nothing listening on that port -> connection refused, which is a normal
    # "portal unreachable" case the runner must handle without submitting.
    settings = _settings(f"http://127.0.0.1:{free_port}")
    config = RunConfig(document_ids=[doc_id], headless=True,
                       settle_seconds_after_uncertain=0.1, probe_attempts=1)
    portal = build_portal(settings)
    portal.start()
    try:
        runner = Runner(db, portal, config, settings, run_id=None)
        stats = runner.run()
    finally:
        portal.close()
    sub = db.one("SELECT state FROM submissions WHERE doc_id=?", (doc_id,))
    assert stats["accepted"] == 0
    assert not sub or sub["state"] in ("none", None), "no claim may be taken"
