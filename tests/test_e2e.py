"""End-to-end browser tests: record, session expiry, crash/kill safety, duplicates."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from declarant.common import connect, connect_mock, init_db


pytestmark = pytest.mark.e2e


def _approve_rate(monkeypatch, rate: float = 0.0):
    from mock_system import app as mock_app
    monkeypatch.setattr(mock_app, "REJECT_RATE", rate)
    monkeypatch.setattr(mock_app, "REJECT_RATE_REVISION", 0.0)


def _decl(imported):
    from declarant.importer.service import load_declaration
    inv = imported["report"]["invoices"][0]
    return load_declaration(inv["declaration_id"])


def test_full_record_and_reconcile(declarant, imported, monkeypatch, mock_db_rows):
    _approve_rate(monkeypatch)
    from declarant.worker.engine import run_single
    d = _decl(imported)
    result = run_single(d["id"], headless=True)
    assert result["ok"], result
    assert result["status"] == "approved"
    decl_no = result["decl_no"]
    assert len(decl_no) == 18

    conn = connect()
    try:
        row = conn.execute("SELECT * FROM declarations WHERE id=?", (d["id"],)).fetchone()
        assert row["status"] == "approved"
        assert row["mock_decl_id"]
        recon = conn.execute("SELECT * FROM reconciliation_run WHERE declaration_id=?",
                             (d["id"],)).fetchone()
        assert recon["mismatched"] == 0
        ledger = [dict(r) for r in conn.execute("SELECT * FROM submit_ledger")]
        assert len(ledger) == 1 and ledger[0]["phase"] == "approved"
    finally:
        conn.close()
    rows = mock_db_rows()
    assert len(rows) == 1 and rows[0]["status"] == "approved"


def test_reimport_after_approval_creates_nothing(declarant, imported, monkeypatch,
                                                 mock_db_rows, samples_dir):
    _approve_rate(monkeypatch)
    from declarant.worker.engine import run_single
    from declarant.importer.service import import_file
    d = _decl(imported)
    assert run_single(d["id"], headless=True)["ok"]
    again = import_file(samples_dir / "invoice_CI-202610-0002.pdf", "again.pdf")
    assert again["duplicate"] == 1 and again["imported"] == 0
    assert len(mock_db_rows()) == 1


def test_second_record_of_same_declaration_does_not_resubmit(declarant, imported, monkeypatch,
                                                              mock_db_rows):
    _approve_rate(monkeypatch)
    from declarant.worker.engine import run_single
    d = _decl(imported)
    first = run_single(d["id"], headless=True)
    assert first["status"] == "approved"
    conn = connect()
    conn.execute("UPDATE declarations SET status='validated', approved_at=NULL WHERE id=?",
                 (d["id"],))
    conn.commit()
    conn.close()
    second = run_single(d["id"], headless=True)
    assert second["ok"]
    rows = mock_db_rows()
    assert len(rows) == 1
    # the mock keeps exactly one submit_attempts row -> only one real submission
    conn = connect_mock()
    try:
        attempts = conn.execute("SELECT COUNT(*) n FROM submit_attempts WHERE outcome='SUBMITTED'"
                                ).fetchone()["n"]
        assert attempts == 1
    finally:
        conn.close()


def test_session_expiry_mid_items_resumes_from_current_line(declarant, imported,
                                                             monkeypatch, mock_db_rows):
    """Expire the server session while line 2 is being saved.

    The worker must log back in, replay line 2, and continue with 3 and 4 —
    without creating a second declaration or double-saving a line.
    """
    _approve_rate(monkeypatch)
    import urllib.request

    from declarant.worker import client as client_mod
    from declarant.worker.engine import run_single

    d = _decl(imported)
    original_save = client_mod.MockCustomsClient._save_item
    state = {"expired": False, "saved_seqs": []}

    def expiring_save(self, seq):
        if seq == 2 and not state["expired"]:
            state["expired"] = True
            urllib.request.urlopen(self.base_url + "/api/test/expire-all").read()
            self.log("warn", "forced_expiry", "模拟会话超时（第 2 项保存时）")
        result = original_save(self, seq)
        state["saved_seqs"].append(seq)
        return result

    monkeypatch.setattr(client_mod.MockCustomsClient, "_save_item", expiring_save)
    result = run_single(d["id"], headless=True)
    assert state["expired"], "expiry was not injected"
    assert result["ok"], result
    assert result["status"] == "approved"
    conn = connect()
    try:
        logs = [r["event"] for r in conn.execute(
            "SELECT event FROM job_logs ORDER BY id").fetchall()]
        assert any("item_retry" in e for e in logs), logs
        session = conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        assert session["renewed_count"] >= 1
        job = conn.execute("SELECT * FROM jobs WHERE declaration_id=?", (d["id"],)).fetchone()
        assert job["status"] == "done"
    finally:
        conn.close()
    # exactly one line-2 save reached the server after re-login
    conn = connect_mock()
    try:
        rows = conn.execute(
            "SELECT data_json FROM declaration_items WHERE declaration_id=1 ORDER BY seq_no"
        ).fetchall()
        assert len(rows) == 4
        assert json.loads(rows[1]["data_json"])["goods_name"] == "钢铁支架"
        assert conn.execute("SELECT COUNT(*) n FROM declarations").fetchone()["n"] == 1
    finally:
        conn.close()


def test_kill_during_submit_never_duplicates(declarant, imported, monkeypatch, mock_db_rows,
                                             tmp_path):
    """Kill the worker process right after the submit click; restart must not resubmit."""
    _approve_rate(monkeypatch)
    d = _decl(imported)
    base = os.environ["DECLARANT_DATA_DIR"]
    script = tmp_path / "crash_worker.py"
    script.write_text(
        "import os, sys\n"
        "from declarant.worker import client as c\n"
        "orig = c.MockCustomsClient.submit\n"
        "def boom(self, decl):\n"
        "    # emulate the browser clicking submit and the process dying immediately after\n"
        "    import urllib.request, json\n"
        "    self.page.goto(self.base_url + '/declarations/' + str(decl['mock_decl_id']) + '/submit')\n"
        "    with self.page.expect_navigation():\n"
        "        self.page.click('#submit-decl')\n"
        "    os._exit(137)\n"
        "c.MockCustomsClient.submit = boom\n"
        "from declarant.worker.engine import run_single\n"
        "run_single(%d, headless=True)\n" % d["id"], encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
    proc = subprocess.run([sys.executable, str(script)], env=env, timeout=180,
                          capture_output=True, cwd=str(Path(__file__).resolve().parent.parent))
    assert proc.returncode == 137, proc.stderr.decode()[-2000:]
    rows = mock_db_rows()
    assert len(rows) == 1, rows
    submitted_already = rows[0]["status"] in ("approved", "submitted")

    from declarant.worker.engine import run_single
    # this run must not submit again; it should reconcile/record the existing result
    result = run_single(d["id"], headless=True)
    assert result["ok"], result
    rows_after = mock_db_rows()
    assert len(rows_after) == 1
    conn = connect_mock()
    try:
        n = conn.execute("SELECT COUNT(*) n FROM submit_attempts WHERE outcome='SUBMITTED'"
                         ).fetchone()["n"]
        assert n == 1, f"duplicate submission detected ({n} attempts)"
    finally:
        conn.close()
    assert submitted_already


def test_duplicate_guard_when_mock_already_approved(declarant, imported, monkeypatch,
                                                     mock_db_rows):
    """Local DB gets wiped (simulating a lost sqlite file); worker must still not re-create."""
    _approve_rate(monkeypatch)
    from declarant.worker.engine import run_single
    d = _decl(imported)
    assert run_single(d["id"], headless=True)["status"] == "approved"
    conn = connect()
    try:
        conn.execute("DELETE FROM submit_ledger")
        conn.execute("UPDATE declarations SET status='validated', mock_decl_id=NULL, "
                     "approved_at=NULL WHERE id=?", (d["id"],))
        conn.commit()
    finally:
        conn.close()
    from declarant.worker.engine import run_single as run2
    result = run2(d["id"], headless=True)
    assert result["ok"]
    rows = mock_db_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "approved"


def test_rejection_parse_and_repair(declarant, imported, monkeypatch, mock_db_rows):
    _approve_rate(monkeypatch, 0.0)
    from mock_system import app as mock_app
    from declarant.worker.engine import run_single
    from declarant.worker import rejections

    forced = {"code": "PRICE", "reason": "[价格不符] 第1项商品总价与数量×单价不一致（差额 12.00），请更正成交价格。"}
    monkeypatch.setattr(mock_app, "make_reject_reason", lambda *a, **k: (forced["code"], forced["reason"]))
    monkeypatch.setattr(mock_app, "REJECT_RATE", 1.0)
    d = _decl(imported)
    result = run_single(d["id"], headless=True)
    assert result["ok"] and result["status"] == "rejected"
    conn = connect()
    try:
        rej = conn.execute("SELECT * FROM rejections").fetchone()
        assert rej["category"] == "PRICE"
        assert rej["status"] == "open"
    finally:
        conn.close()
    parsed = rejections.parse_reason(forced["reason"])
    assert parsed["category"] == "PRICE" and parsed["item_seq"] == 1
    # auto repair brings the declaration back to the queue without resubmitting
    from declarant.web.app import app as web_app
    client = web_app.test_client()
    rej_id = conn = None
    conn2 = connect()
    try:
        rej_id = conn2.execute("SELECT id FROM rejections").fetchone()["id"]
    finally:
        conn2.close()
    resp = client.post(f"/rejections/{rej_id}/repair", follow_redirects=True)
    assert resp.status_code == 200
    conn3 = connect()
    try:
        row = conn3.execute("SELECT status FROM declarations WHERE id=?", (d["id"],)).fetchone()
        assert row["status"] == "validated"
        repair_row = conn3.execute("SELECT status FROM rejections WHERE id=?", (rej_id,)).fetchone()
        assert repair_row["status"] == "resolved"
        job = conn3.execute("SELECT status FROM jobs WHERE declaration_id=?",
                            (d["id"],)).fetchone()
        assert job["status"] == "pending"
    finally:
        conn3.close()
    # now approve the resubmission and confirm the original declaration number is kept
    monkeypatch.setattr(mock_app, "REJECT_RATE", 0.0)
    result2 = run_single(d["id"], headless=True)
    assert result2["status"] == "approved"
    assert result2["decl_no"] == result["decl_no"]
    conn4 = connect_mock()
    try:
        attempts = conn4.execute("SELECT outcome FROM submit_attempts ORDER BY id").fetchall()
        assert [a["outcome"] for a in attempts] == ["SUBMITTED", "REJECTED", "SUBMITTED", "APPROVED"]
        decls = conn4.execute("SELECT COUNT(*) n FROM declarations").fetchone()["n"]
        assert decls == 1
    finally:
        conn4.close()


def test_rejected_declaration_requires_revision_and_keeps_number(declarant, imported,
                                                                 monkeypatch, mock_db_rows):
    """A rejection must bump the submit revision so a resubmission is a new intent,
    while the customs declaration number stays the same."""
    from mock_system import app as mock_app
    from declarant.common import connect
    from declarant.worker.engine import run_single

    monkeypatch.setattr(mock_app, "REJECT_RATE", 1.0)
    monkeypatch.setattr(mock_app, "REJECT_RATE_REVISION", 0.0)
    d = _decl(imported)
    first = run_single(d["id"], headless=True)
    assert first["status"] == "rejected"
    conn = connect()
    try:
        row = conn.execute("SELECT reject_attempts FROM declarations WHERE id=?",
                           (d["id"],)).fetchone()
        assert row["reject_attempts"] == 1
    finally:
        conn.close()
    # manual edit of a rejected declaration requeues it without extra clicks
    from declarant.web import app as web
    web.app.config["TESTING"] = True
    with web.app.test_client() as wc:
        wc.post(f"/declarations/{d['id']}/edit",
                data={"item_1_goods_name": "便携式检测仪", "item_1_hs_code": "9031809090"},
                follow_redirects=True)
    monkeypatch.setattr(mock_app, "REJECT_RATE", 0.0)
    second = run_single(d["id"], headless=True)
    assert second["status"] == "approved"
    assert second["decl_no"] == first["decl_no"]
    conn = connect_mock()
    try:
        outcomes = [r["outcome"] for r in conn.execute(
            "SELECT outcome FROM submit_attempts ORDER BY id").fetchall()]
        assert outcomes == ["SUBMITTED", "REJECTED", "SUBMITTED", "APPROVED"]
        assert conn.execute("SELECT COUNT(*) n FROM declarations").fetchone()["n"] == 1
    finally:
        conn.close()


def test_kill_before_submit_click_recovers_once(declarant, imported, monkeypatch,
                                               mock_db_rows, tmp_path):
    """Kill the worker after writing the intent but before clicking submit.

    The next run must take over the stale intent and submit exactly once.
    """
    _approve_rate(monkeypatch)
    from declarant.common import connect
    from declarant.worker import engine

    d = _decl(imported)
    conn = connect()
    try:
        conn.execute("INSERT INTO submit_ledger(declaration_id,fingerprint,revision,phase,"
                     "mock_decl_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (d["id"], d["fingerprint"], 0, "intent", "", "2020-01-01 00:00:00",
                      "2020-01-01 00:00:00"))
        conn.commit()
    finally:
        conn.close()
    result = engine.run_single(d["id"], headless=True)
    assert result["ok"] and result["status"] == "approved", result
    assert len(mock_db_rows()) == 1
    conn = connect_mock()
    try:
        assert conn.execute("SELECT COUNT(*) n FROM submit_attempts WHERE outcome='SUBMITTED'"
                            ).fetchone()["n"] == 1
    finally:
        conn.close()
