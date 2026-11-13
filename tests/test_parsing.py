"""Parsing, normalisation and validation unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.importer.base import (
    ParsedItem, fingerprint, split_invoice, validate_invoice,
)
from app.importer import excel as excel_parser
from app.importer import pdf as pdf_parser
from common.formatting import parse_elements, parse_mode, to_decimal, values_equal


def test_parse_mode_variants():
    assert parse_mode("一般贸易(0100)") == "0100"
    assert parse_mode("0100") == "0100"
    assert parse_mode("进料对口") == "0615"
    assert parse_mode("others") == ""


def test_to_decimal_handles_currency_symbols():
    assert to_decimal("US$1,234.50") == to_decimal("1234.5")
    assert to_decimal("￥ 88") == to_decimal("88")
    assert to_decimal("") is None


def test_values_equal_tolerates_noise():
    assert values_equal("quantity", "10", "10.0000")
    assert values_equal("total_price", "5000", "5,000.00")
    assert values_equal("trade_mode", "一般贸易(0100)", "0100")
    assert not values_equal("quantity", "10", "11")


def test_parse_elements_recovers_wrapped_pairs():
    raw = "品牌类型=无品牌;出口享惠情况=出口货物在最终目的国享受优惠关税品牌=LENOVO;型号=T95"
    keys = ["品牌类型", "出口享惠情况", "品牌", "型号"]
    parsed = parse_elements(raw, keys)
    assert parsed["品牌"] == "LENOVO"
    assert parsed["型号"] == "T95"


def test_excel_splits_invoices(sample_dir):
    invoices = excel_parser.parse_excel(sample_dir / "客户发票_华东_20261113.xlsx")
    refs = [inv.external_ref for inv in invoices]
    assert "INV-2026-1001" in refs
    assert len(refs) == 6
    for inv in invoices:
        assert inv.header["currency"] in ("USD", "EUR")
        assert inv.items and all(it.hs_code.isdigit() for it in inv.items)


def test_pdf_parses_header_and_items(sample_dir):
    invoices = pdf_parser.parse_pdf(sample_dir / "客户发票_华南_20261113.pdf")
    assert len(invoices) == 2
    first = invoices[0]
    assert first.header["trade_mode"].startswith(("0100", "0214", "0615", "1039", "1210"))
    assert first.items
    item = first.items[0]
    assert item.unit and item.quantity and item.total_price
    assert item.elements, "PDF 申报要素应当被解析"
    assert item.origin_country, "PDF 原产国应当被解析"


def test_split_invoice_renumbers_and_marks_source():
    items = [ParsedItem(item_no=i, hs_code="8471301000", goods_name="笔记本电脑",
                        spec_model="M", quantity="1", unit="台", unit_price="1.00",
                        total_price="1.00", currency="USD", origin_country="中国",
                        destination_country="美国", elements={"品牌类型": "无品牌"})
             for i in range(1, 26)]
    base = type("Inv", (), {
        "external_ref": "INV-1",
        "header": {"external_ref": "INV-1", "currency": "USD"},
        "source_kind": "excel", "source_file": "f.xlsx", "page_ref": "sheet1",
        "items": items, "raw_warnings": [], "sequence_no": 0,
    })()
    split = split_invoice(base, max_items=20)
    assert len(split) == 2
    assert split[0].header["external_ref"] == "INV-1-01"
    assert len(split[0].items) == 20
    assert len(split[1].items) == 5
    assert [it.item_no for it in split[1].items] == [1, 2, 3, 4, 5]
    assert "拆分" in split[0].header["remark"]


def _valid_item(**overrides):
    item = ParsedItem(item_no=1, hs_code="8471301000", goods_name="便携式笔记本电脑",
                      spec_model="T14", quantity="10", unit="台", unit_price="500.00",
                      total_price="5000.00", currency="USD", origin_country="中国",
                      destination_country="美国",
                      elements={"品牌类型": "无品牌", "出口享惠情况": "不能确定",
                                "品牌": "LENOVO", "型号": "T14"})
    for k, v in overrides.items():
        setattr(item, k, v)
    return item


def _valid_header(**overrides):
    h = {"external_ref": "INV-1", "trade_mode": "0100", "declaration_type": "出口",
         "port": "上海洋山港", "consignor": "A公司", "consignee": "B公司",
         "destination_country": "美国", "currency": "USD", "license_no": "L1",
         "freight": "10.00", "insurance": "2.00"}
    h.update(overrides)
    return h


def test_valid_invoice_passes():
    result = validate_invoice(_valid_header(), [_valid_item()])
    assert result["ok"], result["errors"]


def test_retired_hs_code_is_blocking():
    result = validate_invoice(_valid_header(), [_valid_item(hs_code="8471300000")])
    assert not result["ok"]
    assert any(e["code"] == "I002" for e in result["errors"])


def test_unknown_hs_code_is_blocking():
    result = validate_invoice(_valid_header(), [_valid_item(hs_code="9999999999")])
    assert any(e["code"] == "I003" for e in result["errors"])


def test_bad_unit_is_blocking():
    result = validate_invoice(_valid_header(), [_valid_item(unit="米")])
    assert any(e["code"] == "I004" for e in result["errors"])


def test_total_price_mismatch_is_blocking():
    result = validate_invoice(_valid_header(), [_valid_item(total_price="5001.00")])
    assert any(e["code"] == "I017" for e in result["errors"])


def test_missing_element_is_blocking():
    item = _valid_item()
    item.elements.pop("品牌类型")
    result = validate_invoice(_valid_header(), [item])
    assert any(e["code"] == "I007" and "品牌类型" in e["message"]
               for e in result["errors"])


def test_invalid_element_vocabulary_is_blocking():
    item = _valid_item()
    item.elements["品牌类型"] = "自有品牌"
    result = validate_invoice(_valid_header(), [item])
    assert any(e["code"] == "I008" for e in result["errors"])


def test_currency_mismatch_between_header_and_item():
    item = _valid_item(currency="CNY")
    result = validate_invoice(_valid_header(), [item])
    assert any(e["code"] == "I013" for e in result["errors"])


def test_missing_dynamic_field_for_trade_mode():
    header = _valid_header(trade_mode="0214", processing_agreement_no="")
    result = validate_invoice(header, [_valid_item()])
    assert any(e["code"] == "H003" and "加工贸易协议号" in e["message"]
               for e in result["errors"])


def test_fingerprint_changes_with_data():
    items = [_valid_item()]
    fp1 = fingerprint(_valid_header(), items)
    items2 = [_valid_item(quantity="11", total_price="5500.00")]
    assert fingerprint(_valid_header(), items2) != fp1
