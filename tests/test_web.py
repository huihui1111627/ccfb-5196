"""Web console smoke + workflow tests (Flask test client, no browser)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest


@pytest.fixture()
def client(declarant):
    from declarant.web.app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_pages_render(client):
    for url in ["/", "/queue", "/jobs", "/reconciliation", "/rejections", "/stats",
                "/settings", "/health"]:
        resp = client.get(url)
        assert resp.status_code == 200, url


def test_upload_import_via_console(client, samples_dir):
    with open(samples_dir / "invoice_CI-202609-0001.pdf", "rb") as f:
        data = {"files": (io.BytesIO(f.read()), "invoice.pdf")}
        resp = client.post("/import", data=data, content_type="multipart/form-data",
                           follow_redirects=True)
    assert resp.status_code == 200
    assert "导入完成" in resp.get_data(as_text=True)
    # uploading the same invoice again must not create a second declaration
    with open(samples_dir / "invoice_CI-202609-0001.pdf", "rb") as f:
        data = {"files": (io.BytesIO(f.read()), "invoice2.pdf")}
        resp = client.post("/import", data=data, content_type="multipart/form-data",
                           follow_redirects=True)
    assert "重复跳过 1" in resp.get_data(as_text=True)


def test_edit_and_validate_flow(client, samples_dir):
    """Break a declaration, confirm it holds, fix it, confirm it re-enters the queue."""
    from declarant.common import connect
    from declarant.importer.service import import_file, load_declaration
    res = import_file(samples_dir / "invoice_CI-202610-0002.pdf", "x.pdf")
    decl_id = res["report"]["invoices"][0]["declaration_id"]
    d = load_declaration(decl_id)
    resp = client.post(f"/declarations/{decl_id}/edit", data={
        "item_1_goods_name": "便携式检测仪",
        "item_1_hs_code": "9999999999",
        # linked trade-mode fields must survive an edit round-trip
        "processing_ratio": "60",
        "bonded_register_no": d["bonded_register_no"],
    }, follow_redirects=True)
    assert resp.status_code == 200
    d = load_declaration(decl_id)
    assert d["status"] == "hold"
    assert any("不在本地税则库" in i["message"] for i in d["issues"])
    # fix HS and restore elements; the page validates and requeues
    conn = connect()
    try:
        conn.execute("UPDATE declaration_items SET hs_code='9031809090', elements=? "
                     "WHERE declaration_id=? AND seq_no=1",
                     (json.dumps({"brand_type": "境外品牌(其他)", "use": "工业检测",
                                  "principle": "红外测温", "model": "FX-500"},
                                 ensure_ascii=False), decl_id))
        conn.commit()
    finally:
        conn.close()
    resp = client.post(f"/declarations/{decl_id}/validate", follow_redirects=True)
    assert "校验通过" in resp.get_data(as_text=True)
    d = load_declaration(decl_id)
    assert d["status"] == "validated"


def test_stats_and_export(client):
    from declarant.common import connect, now_iso, today_str
    conn = connect()
    try:
        conn.execute("INSERT INTO daily_stats(day, imported_count, approved_count, rejected_count,"
                     " submitted_count, updated_at) VALUES(?,?,?,?,?,?) "
                     "ON CONFLICT(day) DO UPDATE SET approved_count=3, rejected_count=1,"
                     " imported_count=5, submitted_count=4, updated_at=excluded.updated_at",
                     (today_str(), 5, 3, 1, 4, now_iso()))
        conn.commit()
    finally:
        conn.close()
    resp = client.get("/stats")
    assert "总体成功率" in resp.get_data(as_text=True)
    resp = client.get("/export/stats.xlsx")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(resp.data))
    assert {"每日统计", "报关单明细", "退单分析"} <= set(wb.sheetnames)


def test_rejection_manual_requeue(client, samples_dir, declarant):
    from declarant.common import connect
    from declarant.importer.service import import_file
    res = import_file(samples_dir / "invoice_CI-202609-0001.pdf", "r.pdf")
    decl_id = res["report"]["invoices"][0]["declaration_id"]
    conn = connect()
    try:
        conn.execute("INSERT INTO rejections(declaration_id,mock_decl_id,decl_no,raw_reason,"
                     "category,field_path,item_seq,suggestion,status,created_at,updated_at)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (decl_id, "9", "2231X", "[要素不符] 第1项商品申报要素[用途]填报不一致。",
                      "ELEMENT", "items[1].elements.use", 1, "补正要素", "open",
                      "2026-11-13 10:00:00", "2026-11-13 10:00:00"))
        conn.execute("UPDATE declarations SET status='rejected' WHERE id=?", (decl_id,))
        conn.commit()
    finally:
        conn.close()
    resp = client.get("/rejections")
    assert "要素不符" in resp.get_data(as_text=True)
    resp = client.post("/rejections/1/requeue", follow_redirects=True)
    assert "已重新排队" in resp.get_data(as_text=True)
    conn = connect()
    try:
        assert conn.execute("SELECT status FROM declarations WHERE id=?",
                            (decl_id,)).fetchone()["status"] == "validated"
    finally:
        conn.close()
