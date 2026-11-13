"""Parser + validation + import idempotency tests (no browser)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from declarant.common import catalog, connect, init_db, validate_item
from declarant.importer import parsers


def test_parse_pdf_invoice(samples_dir):
    parsed = parsers.parse_file(samples_dir / "invoice_CI-202610-0002.pdf")
    assert len(parsed) == 1
    inv = parsed[0].invoice
    assert inv["invoice_no"] == "CI-202610-0002"
    assert inv["trade_mode"] == "0615"
    assert inv["trade_mode_label"] == "进料对口"
    assert len(inv["items"]) == 4
    assert inv["currency"] == "USD"
    assert inv["total_amount"] == pytest.approx(191068.0)
    first = inv["items"][0]
    assert first["hs_code"] == "9031809090"
    assert first["unit"] == "台"
    assert first["legal_quantity"] == 300
    assert first["elements"]["brand_type"] == "境外品牌(其他)"
    assert str(inv["bonded_register_no"]).startswith("B-")


def test_parse_excel_splits_sheets(samples_dir):
    parsed = parsers.parse_file(samples_dir / "invoices_7sheets.xlsx")
    assert len(parsed) == 7
    invs = {p.invoice["invoice_no"] for p in parsed}
    assert len(invs) == 7
    assert all(p.items for p in parsed)


def test_parse_excel_multi_invoice_per_sheet(samples_dir):
    parsed = parsers.parse_file(samples_dir / "invoices_multi.xlsx")
    assert len(parsed) == 2
    assert {p.invoice["trade_mode"] for p in parsed} == {"2600", "0615"}


def test_parser_normalises_units_and_hs(samples_dir):
    for p in parsers.parse_file(samples_dir / "invoices_7sheets.xlsx"):
        for it in p.items:
            assert len(it["hs_code"]) == 10 and it["hs_code"].isdigit()
            assert " " not in (it["unit"] or "")
            assert it["origin_country"] in catalog()["countries"]


def test_validation_catches_bad_hs_and_missing_elements():
    item = {"seq_no": 1, "hs_code": "123", "quantity": 1, "unit": "台",
            "total_price": 10, "origin_country": "USA", "elements": {}}
    issues = validate_item(item)
    assert any("10位" in i.message for i in issues)

    item2 = {"seq_no": 2, "hs_code": "8471300000", "quantity": 1, "unit": "台",
             "legal_quantity": 1, "unit_price": 1, "total_price": 1,
             "origin_country": "JPN", "elements": {}}
    messages = [i.message for i in validate_item(item2)]
    assert any("品牌类型" in m for m in messages)


def test_validation_detects_price_mismatch():
    item = {"seq_no": 1, "hs_code": "8471300000", "quantity": 10, "unit": "台",
            "legal_quantity": 10, "unit_price": 100, "total_price": 1200,
            "origin_country": "JPN",
            "elements": {"brand_type": "无品牌",
                         "export_benefit": "出口货物在最终目的国不享受优惠关税",
                         "use": "办公", "brand": "A", "model": "B"}}
    messages = [i.message for i in validate_item(item)]
    assert any("不一致" in m for m in messages)


def test_import_is_idempotent(declarant, samples_dir):
    from declarant.importer.service import import_file
    first = import_file(samples_dir / "invoice_CI-202610-0002.pdf", "a.pdf")
    assert first["imported"] == 1
    second = import_file(samples_dir / "invoice_CI-202610-0002.pdf", "b.pdf")
    assert second["imported"] == 0
    assert second["duplicate"] == 1
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) n FROM declarations").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) n FROM import_batches").fetchone()["n"] == 2
    finally:
        conn.close()


def test_import_holds_invalid_invoice(declarant, tmp_path):
    """An invoice with a bogus HS code must go to hold, not into the queue."""
    import openpyxl
    from declarant.importer.service import import_file, load_declaration

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["商业发票 COMMERCIAL INVOICE"])
    ws.append(["发票号", "BAD-0001"])
    ws.append(["贸易方式", "0110 一般贸易"])
    ws.append(["申报地海关", "2201"])
    ws.append(["运输方式", "WATER"])
    ws.append(["币制", "USD"])
    ws.append(["包装种类", "CARTON"])
    ws.append([])
    ws.append(["项号", "HS编码", "商品名称", "数量", "单位", "单价", "总价", "原产国"])
    ws.append([1, "9999999999", "未知商品", 10, "台", 5, 50, "USA"])
    path = tmp_path / "bad.xlsx"
    wb.save(path)
    res = import_file(path, "bad.xlsx")
    assert res["invalid"] == 1
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM declarations").fetchone()
        assert row["status"] == "hold"
        decl = load_declaration(row["id"])
        assert any("不在本地税则库" in i["message"] for i in decl["issues"])
        # held declarations must not have a record job
        assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 0
    finally:
        conn.close()
