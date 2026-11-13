"""Parse commercial invoices from Excel workbooks and PDF files.

Returns normalised invoice payloads that the rest of the system (validation,
queue, RPA worker) consumes. One file may contain several invoices.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from declarant.common import catalog, hs_entry, normalize_hs_code

# label -> internal field. Both Chinese and English variants accepted.
META_LABELS: dict[str, str] = {
    "发票号": "invoice_no", "invoice no.": "invoice_no", "invoice no": "invoice_no",
    "发票号码": "invoice_no",
    "合同号": "contract_no", "contract no.": "contract_no", "contract no": "contract_no",
    "合同协议号": "contract_no",
    "贸易方式": "trade_mode", "trade mode": "trade_mode",
    "供货方": "supplier", "supplier": "supplier", "出口商": "supplier",
    "采购方": "buyer", "buyer": "buyer", "进口商": "buyer", "收货人": "buyer",
    "成交方式": "trade_terms", "term": "trade_terms", "terms": "trade_terms",
    "币制": "currency", "currency": "currency",
    "启运国": "origin_country", "origin": "origin_country", "原产国": "origin_country",
    "运抵国": "dest_country", "destination": "dest_country",
    "申报地海关": "port_code", "port": "port_code",
    "运输方式": "transport_mode", "transport": "transport_mode",
    "运输工具": "vessel_voyage", "vessel/voyage": "vessel_voyage",
    "包装件数": "package_count", "packages": "package_count",
    "包装种类": "packing_type", "packing": "packing_type",
    "毛重": "gross_weight", "gross weight": "gross_weight",
    "净重": "net_weight", "net weight": "net_weight",
    "发票日期": "invoice_date", "date": "invoice_date",
    "加工贸易比例(%)": "processing_ratio", "加工贸易比例": "processing_ratio",
    "加工贸易手册号": "bonded_register_no",
    "保税仓库号": "warehouse_no", "仓储期限(天)": "storage_days",
    "电商订单号": "ecommerce_order_no", "电商平台": "ecommerce_platform",
    "担保编号": "guarantee_no", "担保金额": "guarantee_amount",
    "原报关单号": "original_decl_no", "退运原因": "return_reason",
    "租赁期限(月)": "lease_term_months",
    "总价": "total_amount", "发票总价": "total_amount", "total amount": "total_amount",
    "总金额": "total_amount",
}

ITEM_HEADER_KEYS: dict[str, str] = {
    "项号": "seq", "序号": "seq", "hs编码": "hs_code", "hs code": "hs_code",
    "商品编码": "hs_code", "商品名称": "goods_name", "品名": "goods_name",
    "规格型号": "specification", "规格": "specification", "型号": "model",
    "品牌": "brand", "数量": "quantity", "成交数量": "quantity",
    "单位": "unit", "成交单位": "unit", "单价": "unit_price", "总价": "total_price",
    "金额": "total_price", "原产国": "origin_country", "原产国(地区)": "origin_country",
    "法定数量": "legal_quantity", "法定单位": "legal_unit",
    "第二数量": "second_quantity", "第二单位": "second_unit",
    "申报要素": "elements_inline",
}

ELEMENT_LABEL_TO_KEY: dict[str, str] = {}
for _entry in catalog()["hs_codes"].values():
    for _spec in _entry["elements"]:
        ELEMENT_LABEL_TO_KEY[_spec["label"]] = _spec["key"]

NUM_FIELDS = {"quantity", "legal_quantity", "second_quantity", "unit_price", "total_price",
              "gross_weight", "net_weight", "package_count", "processing_ratio",
              "storage_days", "guarantee_amount", "lease_term_months", "exchange_rate"}


@dataclass
class ParsedInvoice:
    invoice: dict[str, Any]
    items: list[dict[str, Any]]
    source_file: str
    source_kind: str
    source_page: int
    sheet: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"invoice": self.invoice, "items": self.items,
                "source_file": self.source_file, "source_kind": self.source_kind,
                "source_page": self.source_page, "sheet": self.sheet,
                "warnings": self.warnings}


# --------------------------------------------------------------------------
# shared normalisation
# --------------------------------------------------------------------------
def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\n", " ").strip()


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    txt = re.sub(r"[^\d.\-]", "", str(value))
    try:
        return float(txt)
    except ValueError:
        return None


def _label_key(raw: str) -> str | None:
    txt = _clean(raw).lower()
    txt = re.sub(r"\s+", " ", txt)
    txt = re.sub(r"^\d+\s*[.、]\s*", "", txt)
    compact = txt.replace(" ", "")
    for label, key in META_LABELS.items():
        lab = label.lower()
        if txt == lab or compact == lab.replace(" ", ""):
            return key
        if (txt.startswith(lab + " ") or txt.startswith(lab + ":")
                or compact.startswith(lab.replace(" ", "") + ":")
                or compact.startswith(lab.replace(" ", ""))):
            return key
    return None


def _item_key(raw: str) -> str | None:
    txt = _clean(raw).lower()
    txt = re.sub(r"\s+", " ", txt)
    txt = txt.replace("(元)", "").replace("（元）", "")
    compact = txt.replace(" ", "")
    for label, key in ITEM_HEADER_KEYS.items():
        if txt == label or compact == label.replace(" ", ""):
            return key
        if txt.startswith(label) or compact.startswith(label.replace(" ", "")):
            return key
    return None


def _is_item_header(row: Iterable[Any]) -> bool:
    cells = [str(c).lower() for c in row if c]
    joined = " ".join(cells)
    return "hs编码" in joined or "hs code" in joined or ("商品名称" in joined and "数量" in joined)


def _mode_code(value: str) -> str:
    val = _clean(value)
    m = re.match(r"^([0-9]{4})\s*", val)
    if m:
        return m.group(1)
    for item in catalog()["trade_modes"]:
        if item["label"] in val:
            return item["code"]
    return val


def parse_elements(text: str) -> dict[str, str]:
    """Parse 'brand_type=境外品牌(其他)；use=办公' or '品牌类型：境外品牌(其他)'."""
    out: dict[str, str] = {}
    if not text:
        return out
    text = _clean(text)
    chunks = re.split(r"[；;]|\\n", text)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(\w+)\s*=\s*(.*)$", chunk)
        if m:
            out[m.group(1)] = m.group(2).strip()
            continue
        m = re.match(r"^\s*(?:[0-9]+\.)?\s*([^：:=]{1,20})\s*[：:=]\s*(.*)$", chunk)
        if m:
            label = m.group(1).strip()
            key = ELEMENT_LABEL_TO_KEY.get(label, label)
            out[key] = m.group(2).strip()
    return out


def merge_element_texts(*texts: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for t in texts:
        if t:
            merged.update(parse_elements(str(t)))
    return merged


def finalise_invoice(inv: dict[str, Any], items: list[dict[str, Any]],
                     warnings: list[str]) -> dict[str, Any]:
    inv = dict(inv)
    inv["trade_mode"] = _mode_code(inv.get("trade_mode", ""))
    # derived values
    entry = next((m for m in catalog()["trade_modes"] if m["code"] == inv["trade_mode"]), None)
    inv.setdefault("nature", entry["nature"] if entry else "")
    inv["duty_mode"] = entry["duty_mode"] if entry else ""
    inv["trade_mode_label"] = entry["label"] if entry else ""
    if not inv.get("origin_country") and items:
        inv["origin_country"] = items[0].get("origin_country", "")
    # trade country defaults to origin country when the source only has 启运国
    inv["trade_country"] = (inv.get("trade_country") or inv.get("origin_country")
                            or (items[0].get("origin_country", "") if items else ""))
    if not inv.get("port_code"):
        warnings.append("源单缺少申报地海关，导入时保留为空，需人工补录")
    inv["currency"] = inv.get("currency") or "USD"
    try:
        inv["exchange_rate"] = float(inv.get("exchange_rate")
                                     or catalog()["currencies"].get(inv["currency"], {}).get("rate")
                                     or 1)
    except (TypeError, ValueError):
        inv["exchange_rate"] = 1.0
    inv["client_ref"] = inv.get("invoice_no", "")

    norm_items = []
    for seq, raw in enumerate(items, start=1):
        it = dict(raw)
        it["seq_no"] = int(_num(it.get("seq")) or seq)
        it["hs_code"] = _clean(it.get("hs_code"))
        it["hs_code"] = normalize_hs_code(it["hs_code"])
        for fld in NUM_FIELDS:
            if fld in it:
                v = _num(it[fld])
                it[fld] = v
        it["goods_name"] = _clean(it.get("goods_name"))
        it["specification"] = _clean(it.get("specification"))
        it["brand"] = _clean(it.get("brand"))
        it["model"] = _clean(it.get("model"))
        it["unit"] = re.sub(r"\s+", "", _clean(it.get("unit")))
        it["origin_country"] = _clean(it.get("origin_country")).upper()
        it["elements"] = {k: _clean(v) for k, v in (it.get("elements") or {}).items()}
        hs = hs_entry(it["hs_code"])
        if hs:
            it.setdefault("legal_unit", hs.get("legal_unit") or it.get("unit"))
            it["legal_unit"] = re.sub(r"\s+", "", _clean(it.get("legal_unit"))) \
                or hs.get("legal_unit") or ""
            if hs.get("second_unit"):
                it["second_unit"] = re.sub(
                    r"\s+", "", _clean(it.get("second_unit"))) or hs["second_unit"]
            if it.get("legal_quantity") is None:
                qty = it.get("quantity")
                if qty is not None and (it["legal_unit"] == it.get("unit")):
                    it["legal_quantity"] = qty
        if not it.get("origin_country"):
            it["origin_country"] = _clean(inv.get("origin_country")).upper()
        if it.get("unit_price") is None and it.get("total_price") and it.get("quantity"):
            it["unit_price"] = round(float(it["total_price"]) / float(it["quantity"]), 4)
        if it.get("total_price") is None and it.get("unit_price") and it.get("quantity"):
            it["total_price"] = round(float(it["unit_price"]) * float(it["quantity"]), 2)
        norm_items.append(it)
    inv["items"] = norm_items
    inv["item_count"] = len(norm_items)
    total = inv.get("total_amount")
    if total in (None, "", 0):
        inv["total_amount"] = round(sum(float(i.get("total_price") or 0) for i in norm_items), 2)
    else:
        inv["total_amount"] = float(_num(total) or inv["total_amount"])
    return inv


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------
def parse_excel(path: Path) -> list[ParsedInvoice]:
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    results: list[ParsedInvoice] = []
    for ws in wb.worksheets:
        rows = [[c for c in row] for row in ws.iter_rows(values_only=True)]
        results.extend(_parse_rows(rows, path.name, "excel", sheet=ws.title))
    return results


def _parse_rows(rows: list[list[Any]], filename: str, kind: str,
                sheet: str = "", page: int = 0) -> list[ParsedInvoice]:
    blocks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    in_items = False
    items_mode = "table"  # table | elements | none
    header_mapping: list[tuple[int, str]] = []

    def start_block() -> dict[str, Any]:
        return {"invoice": {}, "items": [], "elements_texts": []}

    for row in rows:
        cells = [_clean(c) for c in row]
        nonempty = [c for c in cells if c]
        if not nonempty:
            continue
        joined = " ".join(nonempty)

        # ---- new invoice boundary: title row, or a metadata invoice-no row
        titleish = ("商业发票" in joined or "COMMERCIAL INVOICE" in joined.upper()
                    or "销售发票" in joined)
        inv_label_row = bool(cells) and _label_key(cells[0]) == "invoice_no"
        if (titleish and not in_items and not any(_label_key(c) == "invoice_no" for c in cells)) \
                or (inv_label_row and cur is not None and cur["invoice"].get("invoice_no")):
            if cur is not None and (cur["invoice"] or cur["items"]):
                blocks.append(cur)
            cur = start_block()
            in_items = False
            items_mode = "none"
            header_mapping = []

        if cur is None:
            cur = start_block()

        first = nonempty[0]

        # ---- tail markers (work in every state)
        if "申报要素" in first or "DECLARATION ELEMENT" in first.upper():
            items_mode = "elements"
            in_items = False
            header_mapping = []
            rest = joined[len(first):].strip(" ：:，,")
            if rest:
                cur["elements_texts"].append(rest)
            continue
        if "合计" in first or "TOTAL" in first.upper():
            in_items = False
            items_mode = "none"
            continue

        # ---- item table header
        if items_mode != "elements" and _is_item_header(nonempty):
            in_items = True
            items_mode = "table"
            header_mapping = []
            for i, cell in enumerate(cells):
                key = _item_key(cell)
                if key:
                    header_mapping.append((i, key))
            continue

        if items_mode == "elements":
            if "=" in joined or "：" in joined:
                cur["elements_texts"].append(joined)
            continue

        # ---- item row
        if items_mode == "table" and header_mapping:
            hs_idx = next((i for i, k in header_mapping if k == "hs_code"), None)
            if hs_idx is None or hs_idx >= len(cells):
                continue
            hs_raw = normalize_hs_code(cells[hs_idx])
            if len(hs_raw) < 6:
                continue
            item: dict[str, Any] = {"elements": {}}
            inline_elements = []
            for i, key in header_mapping:
                if i >= len(cells):
                    continue
                val = cells[i]
                if key == "elements_inline":
                    inline_elements.append(val)
                elif key == "seq":
                    item["seq"] = val
                else:
                    item[key] = val
            item["elements"].update(merge_element_texts(*inline_elements))
            cur["items"].append(item)
            continue

        # ---- metadata rows (layout agnostic label/value scan)
        meta_handled = False
        for i, label in enumerate(cells):
            if not label:
                continue
            key = _label_key(label)
            if not key or i + 1 >= len(cells):
                continue
            val = cells[i + 1]
            if not val or (_label_key(val) and key not in ("origin_country", "dest_country")):
                continue
            if key in NUM_FIELDS:
                cur["invoice"][key] = _num(val)
            else:
                cur["invoice"][key] = val
            meta_handled = True

    if cur is not None and (cur["invoice"] or cur["items"]):
        blocks.append(cur)

    results: list[ParsedInvoice] = []
    for b in blocks:
        inv = dict(b["invoice"])
        if not inv.get("invoice_no") or not b["items"]:
            continue
        texts = b.get("elements_texts", [])
        item_refs = {int(_num(it.get("seq")) or i + 1): it for i, it in enumerate(b["items"])}
        for text in texts:
            m = re.match(r"^\s*(\d+)\s*\.?\s*(.*)$", text)
            if m and int(m.group(1)) in item_refs:
                item_refs[int(m.group(1))]["elements"].update(merge_element_texts(m.group(2)))
            elif len(item_refs) == 1:
                next(iter(item_refs.values()))["elements"].update(merge_element_texts(text))
        b["warnings"] = []
        finalised = finalise_invoice(inv, b["items"], b["warnings"])
        results.append(ParsedInvoice(invoice=finalised, items=finalised["items"],
                                     source_file=filename, source_kind=kind,
                                     source_page=page, sheet=sheet, warnings=b["warnings"]))
    return results


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def parse_pdf(path: Path) -> list[ParsedInvoice]:
    import pdfplumber

    results: list[ParsedInvoice] = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables() or []
            text = page.extract_text() or ""
            results.extend(_parse_pdf_page(tables, text, path.name, pno))
    return results


def _parse_pdf_page(tables: list[list[list[str]]], text: str, filename: str,
                    page: int) -> list[ParsedInvoice]:
    blocks: list[dict[str, Any]] = [{"invoice": {}, "items": [], "elements_texts": []}]
    item_header: list[tuple[int, str]] | None = None
    for table in tables:
        if not table:
            continue
        for row in table:
            cells = [_clean(c) for c in (row or [])]
            nonempty = [c for c in cells if c]
            if not nonempty:
                continue
            joined = " ".join(nonempty)
            if _is_item_header(nonempty):
                item_header = []
                for i, cell in enumerate(cells):
                    key = _item_key(cell)
                    if key:
                        item_header.append((i, key))
                continue
            if item_header:
                first = nonempty[0]
                if "合计" in first:
                    item_header = None
                    continue
                hs_idx = next((i for i, k in item_header if k == "hs_code"), None)
                if hs_idx is not None and hs_idx < len(cells) and len(normalize_hs_code(cells[hs_idx])) >= 6:
                    item: dict[str, Any] = {"elements": {}}
                    inline = []
                    for i, key in item_header:
                        if i >= len(cells):
                            continue
                        if key == "elements_inline":
                            inline.append(cells[i])
                        elif key == "seq":
                            item["seq"] = cells[i]
                        else:
                            item[key] = cells[i]
                    item["elements"].update(merge_element_texts(*inline))
                    blocks[-1]["items"].append(item)
                    continue
            if not item_header:
                found = False
                for i, cell in enumerate(cells):
                    key = _label_key(cell)
                    if not key or i + 1 >= len(cells):
                        continue
                    val = cells[i + 1]
                    if not val or _label_key(val):
                        continue
                    blocks[-1]["invoice"][key] = val
                    found = True
                if found:
                    continue
    # element paragraphs from free text
    lines = text.splitlines()
    cur_item: int | None = None
    collected: list[tuple[int | None, str]] = []
    for line in lines:
        if "申报要素" in line:
            rest = line.split("申报要素", 1)[1].strip(" ：:")
            if rest:
                collected.append((None, rest))
            continue
        m = re.match(r"^\s*(\d+)\s*\.\s*(.*(?:=|：).*)", line)
        if m and ("=" in m.group(2) or "：" in m.group(2)):
            collected.append((int(m.group(1)), m.group(2)))
            continue
        if collected and collected[-1][1] and ("=" in line or "：" in line) and not re.match(r"^\s*[0-9]+[\.、]", line):
            collected[-1] = (collected[-1][0], collected[-1][1] + line.strip())
    # rejoin element paragraphs that were wrapped by pdfplumber
    merged: dict[int | None, list[str]] = {}
    for seq, chunk in collected:
        merged.setdefault(seq, []).append(chunk)
    blocks[-1]["elements_texts"] = []  # not used, applied directly
    if blocks[-1]["items"]:
        refs = {int(_num(it.get("seq")) or i + 1): it for i, it in enumerate(blocks[-1]["items"])}
        for seq, chunks in merged.items():
            body = "".join(chunks)
            if seq in refs:
                refs[seq]["elements"].update(merge_element_texts(body))
            elif len(refs) == 1:
                next(iter(refs.values()))["elements"].update(merge_element_texts(body))

    results = []
    for b in blocks:
        inv = b["invoice"]
        if not inv.get("invoice_no") or not b["items"]:
            continue
        finalised = finalise_invoice(inv, b["items"], [])
        results.append(ParsedInvoice(invoice=finalised, items=finalised["items"],
                                     source_file=filename, source_kind="pdf",
                                     source_page=page, warnings=[]))
    return results


def parse_file(path: Path) -> list[ParsedInvoice]:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        return parse_excel(path)
    if suffix == ".pdf":
        return parse_pdf(path)
    raise ValueError(f"不支持的文件类型: {suffix}")


def dump_parsed(parsed: list[ParsedInvoice]) -> str:
    return json.dumps([p.as_dict() for p in parsed], ensure_ascii=False, indent=2)
