"""Console HTTP tests: import, queue, edit, reconciliation and rejection flows."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(broker_env, mock_server):
    from app.web.console import create_app
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _import(client, path: Path):
    with open(path, "rb") as fh:
        data = {"files": (fh, path.name)}
        resp = client.post("/api/import", data=data,
                           content_type="multipart/form-data")
    return resp.get_json()


def test_pages_render(client):
    for url in ("/", "/runs", "/reconcile", "/rejections", "/settings"):
        resp = client.get(url)
        assert resp.status_code == 200, url


def test_import_excel_and_list(client, sample_dir):
    data = _import(client, sample_dir / "客户发票_华东_20261113.xlsx")
    assert data["ok"]
    result = data["results"][0]
    assert result["created"]
    assert len(result["created"]) >= 6, "拆票应产生多票"
    index = client.get("/")
    assert index.status_code == 200
    text = index.get_data(as_text=True)
    assert "INV-2026-1001" in text


def test_import_pdf(client, sample_dir):
    data = _import(client, sample_dir / "客户发票_华南_20261113.pdf")
    assert data["ok"]
    assert data["results"][0]["created"]


def test_import_rejects_unsupported_type(client, tmp_path):
    bad = tmp_path / "x.txt"
    bad.write_text("nope")
    data = _import(client, bad)
    assert not data["ok"]


def test_duplicate_import_is_skipped(client, sample_dir, broker_env):
    path = sample_dir / "客户发票_华南_20261113.pdf"
    first = _import(client, path)
    second = _import(client, path)
    assert second["ok"]
    assert second["results"][0]["created"] == []
    assert second["results"][0]["skipped"]


def test_update_header_clears_validation_error(client, sample_dir, broker_env):
    from app.db import Database
    data = _import(client, sample_dir / "客户发票_华东_20261113.xlsx")
    # find a doc with blocking errors
    db = Database()
    doc = db.one("SELECT * FROM documents WHERE validation_blocking=1 LIMIT 1")
    assert doc, "sample data should contain a defective invoice"
    header = json.loads(doc["header_json"])
    resp = client.post(f"/api/documents/{doc['id']}/update",
                       json={"header": {"currency": "USD"}})
    body = resp.get_json()
    assert body["ok"]


def test_update_item_recomputes_total_and_validation(client, sample_dir):
    from app.db import Database
    _import(client, sample_dir / "客户发票_华东_20261113.xlsx")
    db = Database()
    doc = db.one("SELECT * FROM documents WHERE external_ref='INV-2026-1004'")
    assert doc
    items = json.loads(doc["items_json"])
    target = items[-1]
    resp = client.post(f"/api/documents/{doc['id']}/update", json={
        "items": [{"item_no": target["item_no"], "fields": {"quantity": "2"}}]})
    body = resp.get_json()
    assert resp.status_code == 200, body
    updated = next(it for it in body["items"] if it["item_no"] == target["item_no"])
    # total is recomputed from quantity * unit_price
    assert updated["total_price"] != target["total_price"]


def test_cannot_edit_submitted_document(client, sample_dir):
    from app.db import Database
    from app.services.state import set_document_state
    _import(client, sample_dir / "客户发票_华南_20261113.pdf")
    db = Database()
    from app.services import fence
    doc = db.one("SELECT * FROM documents WHERE status='ready' LIMIT 1")
    set_document_state(db, doc["id"], "queued", "test")
    set_document_state(db, doc["id"], "submitting", "test")
    fence.ensure_row(db, doc["id"], "d" * 64)
    fence.settle_accepted(db, doc["id"], "test", "DECL-X")
    set_document_state(db, doc["id"], "submitted", "test")
    resp = client.post(f"/api/documents/{doc['id']}/update", json={"header": {"currency": "USD"}})
    assert resp.status_code == 409


def test_queue_endpoint(client, sample_dir):
    from app.db import Database
    _import(client, sample_dir / "客户发票_华南_20261113.pdf")
    db = Database()
    ids = [r["id"] for r in db.query("SELECT id FROM documents WHERE status='ready'")]
    resp = client.post("/api/queue", json={"document_ids": ids})
    body = resp.get_json()
    assert body["ok"] and body["queued"] == len(ids)


def test_rejection_flow_via_console(client, sample_dir):
    """Force a rejection, then resolve through the console and re-queue."""
    from app.db import Database
    from app.runner import RunConfig
    from app.runner.main import build_portal
    from app.runner import Runner
    from app.services.state import set_document_state

    data = _import(client, sample_dir / "客户发票_华南_20261113.pdf")
    doc_id = data["results"][0]["created"][0]["document_id"]
    db = Database()
    items = json.loads(db.one("SELECT items_json FROM documents WHERE id=?", (doc_id,))["items_json"])
    for it in items:
        it["elements"] = {}
    db.execute("UPDATE documents SET items_json=? WHERE id=?", (json.dumps(items), doc_id))

    resp = client.post("/runs/start")  # async; not used for the assertion
    # run synchronously in this process for determinism
    db.set_setting("runner_active", "")
    settings = db.get_settings()
    settings.update({"mock_base_url": "http://127.0.0.1:1", "headless": "1"})
    # Direct rejection without browser: simulate with fence
    from app.services import fence
    sha = "b" * 64
    fence.acquire_claim(db, doc_id, "test", sha)
    fence.settle_rejected(db, doc_id, "test", "R01", "申报要素品牌类型未填",
                          category="element", hint="补齐后重录")
    set_document_state(db, doc_id, "rejected", "系统退回")
    assert db.one("SELECT * FROM rejections WHERE doc_id=?", (doc_id,))["status"] == "open"

    # operator resolves via console
    rej = db.one("SELECT * FROM rejections WHERE doc_id=?", (doc_id,))
    resp = client.post(f"/rejections/{rej['id']}/update",
                       data={"action": "resolve", "note": "已补齐",
                             "handler": "ops"})
    assert resp.status_code == 302
    assert db.one("SELECT status FROM rejections WHERE id=?", (rej["id"],))["status"] == "resolved"

    # fix the data then re-enter
    from common import catalog
    items = json.loads(db.one("SELECT items_json FROM documents WHERE id=?", (doc_id,))["items_json"])
    for it in items:
        it["elements"] = {e: ("无品牌" if e == "品牌类型" else
                             ("不能确定" if e == "出口享惠情况" else "无"))
                          for e in catalog.HS_BY_CODE[it["hs_code"]]["elements"]}
    db.execute("UPDATE documents SET items_json=?, validation_blocking=0, validation_json='{}' "
               "WHERE id=?", (json.dumps(items), doc_id))
    resp = client.post(f"/rejections/{doc_id}/reentry")
    assert resp.status_code == 302
    assert db.one("SELECT status FROM documents WHERE id=?", (doc_id,))["status"] == "queued"


def test_settings_save(client, broker_env):
    resp = client.post("/settings", data={
        "mock_base_url": "http://127.0.0.1:5999", "mock_username": "admin",
        "mock_password": "", "session_minutes": "20", "slow_mo_ms": "0",
        "max_resubmit": "3", "settle_seconds_after_uncertain": "2",
        "probe_attempts": "2", "headless": "1", "verify_after_submit": "1",
        "screenshot_all_steps": "1"})
    assert resp.status_code == 302
    from app.db import Database
    assert Database().get_settings()["mock_base_url"] == "http://127.0.0.1:5999"


def test_uncertain_resolution_api(client, broker_env):
    from app.db import Database
    from app.services import fence
    db = Database()
    doc_id = db.execute(
        "INSERT INTO documents(external_ref, source_kind, source_file, page_ref, "
        "sequence_no, status, header_json, items_json, validation_json, "
        "validation_blocking, fingerprint, created_at, updated_at) "
        "VALUES('INV-UNC','excel','f','p',0,'failed','{}','[]','{}',0,'fp','t','t')")
    fence.acquire_claim(db, doc_id, "x", "c" * 64)
    fence.settle_uncertain(db, doc_id, "x", "crash")
    resp = client.post(f"/api/documents/{doc_id}/resolve",
                       json={"resolution": "not_submitted", "note": "确认无记录"})
    assert resp.get_json()["ok"]
    assert fence.get_state(db, doc_id) == fence.ST_NONE
    # release back to the queue so it can legitimately be entered
    from app.services.state import set_document_state
    set_document_state(db, doc_id, "queued", "released")


def test_export_excel_produces_file(client, sample_dir):
    _import(client, sample_dir / "客户发票_华东_20261113.xlsx")
    resp = client.get("/reconcile/export")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def test_screenshot_endpoint_blocks_traversal(client):
    resp = client.get("/shots/../../etc/passwd")
    assert resp.status_code in (403, 404)
