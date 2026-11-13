"""Source-document parsing: Excel invoices, PDF invoices, 拆票 and validation.

Everything downstream works on `ParsedInvoice`, which is already normalised
(field names, currencies, decimals, element dicts) so the Playwright runner never
has to guess at a raw cell.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from common import catalog
from common.formatting import (
    canonical_for_hash,
    clean_text,
    money,
    parse_currency,
    parse_date,
    parse_elements,
    parse_mode,
    quantity,
    to_decimal,
    values_equal,
)
from app.db import json_dumps, now_iso


class SourceParseError(Exception):
    """The file cannot be understood at all."""


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
@dataclass
class ParsedItem:
    item_no: int
    hs_code: str
    goods_name: str
    spec_model: str
    quantity: str
    unit: str
    unit_price: str
    total_price: str
    currency: str
    origin_country: str
    destination_country: str
    elements: dict

    def to_dict(self) -> dict:
        return {
            "item_no": self.item_no,
            "hs_code": self.hs_code,
            "goods_name": self.goods_name,
            "spec_model": self.spec_model,
            "quantity": self.quantity,
            "unit": self.unit,
            "unit_price": self.unit_price,
            "total_price": self.total_price,
            "currency": self.currency,
            "origin_country": self.origin_country,
            "destination_country": self.destination_country,
            "elements": dict(self.elements),
        }


@dataclass
class ParsedInvoice:
    external_ref: str
    source_kind: str
    source_file: str
    page_ref: str
    header: dict
    items: list[ParsedItem]
    raw_warnings: list[str] = field(default_factory=list)
    sequence_no: int = 0


# ---------------------------------------------------------------------------
# Header alias mapping.  Real invoices label the same thing several ways.
# ---------------------------------------------------------------------------
HEADER_ALIASES = {
    "external_ref": ["发票号", "invoice no", "invoice no.", "invoice number", "发票号码",
                     "合同号", "contract no", "contract no.", "单号", "外部单号"],
    "trade_mode": ["贸易方式", "trade mode", "监管方式"],
    "declaration_type": ["申报类型", "进出口", "进出口类型", "declaration type"],
    "port": ["申报口岸", "口岸", "报关口岸", "port", "申报地海关"],
    "consignor": ["境内发货人", "发货人", "境内收发货人", "consignor", "shipper"],
    "consignee": ["境外收货人", "收货人", "consignee", "buyer"],
    "destination_country": ["运抵国", "目的国", "运抵国(地区)", "destination country",
                            "destination", "最终目的国"],
    "currency": ["币制", "currency", "结算币制"],
    "license_no": ["许可证号", "许可证", "license no", "license"],
    "freight": ["运费", "运费(usd)", "freight", "运费/率"],
    "insurance": ["保险费", "保险费(usd)", "insurance"],
    "processing_agreement_no": ["加工贸易协议号", "加工贸易手册号", "协议号",
                                "processing agreement no"],
    "bonded_ratio": ["保税比例", "保税比例(%)", "bonded ratio"],
    "market_purchase_no": ["市场采购单号", "市场采购贸易单号", "market purchase no"],
    "bonded_warehouse_no": ["保税仓单号", "保税仓库单号", "bonded warehouse no"],
    "remark": ["备注", "remark", "remarks"],
}

ITEM_ALIASES = {
    "item_no": ["项号", "序号", "no", "item no"],
    "hs_code": ["hs编码", "hs code", "hs", "商品编码", "税则号列"],
    "goods_name": ["商品名称", "品名", "货物名称", "description", "goods name"],
    "spec_model": ["规格型号", "规格", "型号", "spec", "model"],
    "quantity": ["数量", "quantity", "qty"],
    "unit": ["单位", "成交计量单位", "计量单位", "unit"],
    "unit_price": ["单价", "unit price", "price"],
    "total_price": ["总价", "金额", "total", "amount", "total price"],
    "currency": ["币制", "currency"],
    "origin_country": ["原产国", "原产地", "origin", "country of origin"],
    "destination_country": ["最终目的国", "目的国", "destination country"],
    "elements": ["申报要素", "要素", "declaration elements", "elements"],
}


def _norm_label(text) -> str:
    t = clean_text(text).lower().replace("：", "").replace(":", "").strip()
    t = re.sub(r"[\s\.。、()（）\[\]【】]+", "", t)
    return t


HEADER_LOOKUP: dict[str, str] = {}
for canon, aliases in HEADER_ALIASES.items():
    for a in aliases:
        HEADER_LOOKUP[_norm_label(a)] = canon

ITEM_LOOKUP: dict[str, str] = {}
for canon, aliases in ITEM_ALIASES.items():
    for a in aliases:
        ITEM_LOOKUP[_norm_label(a)] = canon


def match_header_label(text) -> str | None:
    raw = clean_text(text)
    candidate = _norm_label(raw)
    if not candidate:
        return None
    if candidate in HEADER_LOOKUP:
        return HEADER_LOOKUP[candidate]
    akey = _norm_label(re.split(r"[:：]", raw)[0])
    if akey in HEADER_LOOKUP:
        return HEADER_LOOKUP[akey]
    # currency-qualified labels e.g. 运费(USD)
    akey2 = re.sub(r"[a-z()（）]+$", "", akey)
    if akey2 in HEADER_LOOKUP:
        return HEADER_LOOKUP[akey2]
    return None


def match_item_label(text) -> str | None:
    candidate = _norm_label(text)
    if candidate in ITEM_LOOKUP:
        return ITEM_LOOKUP[candidate]
    akey = re.sub(r"[a-z()（）]+$", "", candidate)
    return ITEM_LOOKUP.get(akey)


def split_label_value(line: str) -> tuple[str, str]:
    m = re.search(r"[:：]", line)
    if not m:
        return clean_text(line), ""
    return clean_text(line[:m.start()]), clean_text(line[m.end():])


def normalize_header(raw: dict) -> dict:
    """Map whatever aliases were found onto canonical keys and normalise values."""
    header = {
        "external_ref": "",
        "trade_mode": "",
        "declaration_type": "",
        "port": "",
        "consignor": "",
        "consignee": "",
        "destination_country": "",
        "currency": "",
        "license_no": "",
        "freight": "",
        "insurance": "",
        "processing_agreement_no": "",
        "bonded_ratio": "",
        "market_purchase_no": "",
        "bonded_warehouse_no": "",
        "remark": "",
        "invoice_date": "",
        "contract_no": "",
    }
    for key, value in raw.items():
        canon = match_header_label(key) or (key if key in header else None)
        if canon and canon in header:
            header[canon] = clean_text(value)
    header["trade_mode"] = parse_mode(header["trade_mode"]) or header["trade_mode"]
    if header["declaration_type"]:
        dt = clean_text(header["declaration_type"])
        if "进" in dt:
            header["declaration_type"] = "进口"
        elif "出" in dt:
            header["declaration_type"] = "出口"
    if header["currency"]:
        header["currency"] = parse_currency(header["currency"])
    for money_field in ("freight", "insurance"):
        if header[money_field]:
            d = money(header[money_field])
            header[money_field] = f"{d:.2f}" if d is not None else header[money_field]
    if header["bonded_ratio"]:
        d = to_decimal(header["bonded_ratio"])
        header["bonded_ratio"] = f"{d:f}" if d is not None else header["bonded_ratio"]
    if header["invoice_date"]:
        header["invoice_date"] = parse_date(header["invoice_date"])
    if not header["external_ref"]:
        header["external_ref"] = clean_text(header.get("contract_no", ""))
    return header


def normalize_item(raw: dict, fallback_no: int, header_currency: str = "",
                   known_elements: list[str] | None = None) -> ParsedItem:
    def pick(key: str) -> str:
        return clean_text(raw.get(key, ""))

    qty = quantity(pick("quantity"))
    price = money(pick("unit_price"))
    total = money(pick("total_price"))
    if total is None and qty is not None and price is not None:
        total = money(qty * price)
    if price is None and qty is not None and total is not None and qty != 0:
        price = money(total / qty)
    item_no_raw = pick("item_no")
    try:
        item_no = int(float(item_no_raw)) if item_no_raw else fallback_no
    except ValueError:
        item_no = fallback_no
    elements = parse_elements(raw.get("elements") or "", known_elements)
    return ParsedItem(
        item_no=item_no,
        hs_code=re.sub(r"\D", "", pick("hs_code")),
        goods_name=pick("goods_name"),
        spec_model=pick("spec_model"),
        quantity=f"{qty:f}" if qty is not None else pick("quantity"),
        unit=pick("unit"),
        unit_price=f"{price:.2f}" if price is not None else pick("unit_price"),
        total_price=f"{total:.2f}" if total is not None else pick("total_price"),
        currency=parse_currency(pick("currency")) or header_currency,
        origin_country=pick("origin_country"),
        destination_country=pick("destination_country"),
        elements=elements,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
SEVERITY_BLOCK = "error"
SEVERITY_WARN = "warning"


def validate_invoice(header: dict, items: list[ParsedItem]) -> dict:
    """Return {"errors": [...], "warnings": [...], "ok": bool}.

    Each finding is {field, message, severity, item_no?, code}.
    """
    errors: list[dict] = []
    warnings: list[dict] = []

    def add(sev, message, field_name, item_no=None, code=""):
        entry = {"severity": sev, "field": field_name, "message": message,
                 "item_no": item_no, "code": code}
        (errors if sev == SEVERITY_BLOCK else warnings).append(entry)

    # ---- header ----
    required_header = [
        ("trade_mode", "贸易方式"),
        ("declaration_type", "申报类型"),
        ("port", "申报口岸"),
        ("consignor", "境内发货人"),
        ("consignee", "境外收货人"),
        ("destination_country", "运抵国"),
        ("currency", "币制"),
    ]
    for key, label in required_header:
        if not clean_text(header.get(key, "")):
            add(SEVERITY_BLOCK, f"表头缺少{label}", key, code="H001")
    mode = parse_mode(header.get("trade_mode", ""))
    if header.get("trade_mode") and not mode:
        add(SEVERITY_BLOCK, f"贸易方式“{header['trade_mode']}”无法识别", "trade_mode", code="H002")
    elif mode:
        for f in catalog.required_fields(mode):
            if not clean_text(header.get(f, "")):
                add(SEVERITY_BLOCK,
                    f"{catalog.mode_name(mode)}需填写{catalog.FIELD_LABELS[f]}",
                    f, code="H003")
    if header.get("declaration_type") and header["declaration_type"] not in catalog.DECLARATION_TYPES:
        add(SEVERITY_BLOCK, "申报类型必须为出口或进口", "declaration_type", code="H004")
    cur = parse_currency(header.get("currency", ""))
    if header.get("currency") and cur not in catalog.CURRENCIES:
        add(SEVERITY_BLOCK, f"币制“{header['currency']}”不在支持范围", "currency", code="H005")
    if header.get("destination_country") and header["destination_country"] not in catalog.COUNTRIES:
        add(SEVERITY_WARN, f"运抵国“{header['destination_country']}”不在常用国家表",
            "destination_country", code="H006")
    if not header.get("external_ref"):
        add(SEVERITY_BLOCK, "缺少发票号/合同号，无法建立唯一单号", "external_ref", code="H007")

    # ---- items ----
    if not items:
        add(SEVERITY_BLOCK, "发票中未解析到商品明细", "items", code="I000")

    seen_item_nos: set[int] = set()
    total_calc = 0.0
    for item in items:
        where = item.item_no
        label = item.goods_name or item.hs_code or f"项号{where}"
        if not item.hs_code:
            add(SEVERITY_BLOCK, f"{label} 缺少HS编码", "hs_code", where, "I001")
        elif item.hs_code in catalog.RETIRED_HS_CODES:
            add(SEVERITY_BLOCK,
                f"{label} HS编码{item.hs_code}已失效：{catalog.RETIRED_HS_CODES[item.hs_code]}",
                "hs_code", where, "I002")
        elif item.hs_code not in catalog.HS_BY_CODE and item.hs_code not in catalog.RETIRED_HS_CODES:
            add(SEVERITY_BLOCK, f"{label} HS编码{item.hs_code}不在税则目录", "hs_code", where, "I003")
        elif item.hs_code in catalog.HS_BY_CODE:
            hs = catalog.HS_BY_CODE[item.hs_code]
            if item.unit and item.unit not in hs["valid_units"]:
                add(SEVERITY_BLOCK,
                    f"{label} 单位“{item.unit}”与法定计量单位不符（可用：{'/'.join(hs['valid_units'])}）",
                    "unit", where, "I004")
            if not item.unit:
                add(SEVERITY_BLOCK, f"{label} 缺少成交计量单位", "unit", where, "I005")
            if clean_text(item.goods_name) and hs["name"] not in item.goods_name and \
                    item.goods_name not in hs["name"]:
                add(SEVERITY_WARN,
                    f"{label} 品名与HS目录名称“{hs['name']}”差异较大，请确认归类",
                    "goods_name", where, "I006")
            for element in hs["elements"]:
                if not clean_text(item.elements.get(element, "")):
                    add(SEVERITY_BLOCK,
                        f"{label} 申报要素“{element}”未填写", "elements", where, "I007")
            for element, allowed in catalog.ELEMENT_VOCAB.items():
                if element in item.elements and item.elements[element] and \
                        item.elements[element] not in allowed:
                    add(SEVERITY_BLOCK,
                        f"{label} 申报要素“{element}”取值“{item.elements[element]}”不规范",
                        "elements", where, "I008")
            extra = [k for k in item.elements if k not in hs["elements"]]
            if extra:
                add(SEVERITY_WARN,
                    f"{label} 存在多余申报要素：{', '.join(extra)}", "elements", where, "I009")
        if not item.goods_name:
            add(SEVERITY_BLOCK, f"项号{where} 缺少商品名称", "goods_name", where, "I010")
        if not item.spec_model:
            add(SEVERITY_WARN, f"{label} 未填写规格型号", "spec_model", where, "I011")
        if not item.origin_country:
            add(SEVERITY_WARN, f"{label} 未填写原产国", "origin_country", where, "I012")
        if item.currency and cur and item.currency != cur:
            add(SEVERITY_BLOCK,
                f"{label} 明细币制{item.currency}与表头币制{cur}不一致", "currency", where, "I013")
        qty = to_decimal(item.quantity)
        price = to_decimal(item.unit_price)
        total = to_decimal(item.total_price)
        if qty is None or qty <= 0:
            add(SEVERITY_BLOCK, f"{label} 数量必须为正数", "quantity", where, "I014")
        if price is None or price < 0:
            add(SEVERITY_BLOCK, f"{label} 单价缺失或为负", "unit_price", where, "I015")
        if total is None or total < 0:
            add(SEVERITY_BLOCK, f"{label} 总价缺失或为负", "total_price", where, "I016")
        if qty is not None and price is not None and total is not None:
            expected = (qty * price).quantize(to_decimal("0.01"))
            if abs(expected - total) > to_decimal("0.02"):
                add(SEVERITY_BLOCK,
                    f"{label} 总价{item.total_price}与数量×单价({expected})不符",
                    "total_price", where, "I017")
            total_calc += float(total)
        if item.item_no in seen_item_nos:
            add(SEVERITY_BLOCK, f"项号 {item.item_no} 重复", "item_no", where, "I018")
        seen_item_nos.add(item.item_no)

    if len(items) > 20:
        add(SEVERITY_WARN,
            f"单票明细 {len(items)} 项超过20项，将自动拆分为多票申报", "items", code="I019")

    return {
        "errors": errors,
        "warnings": warnings,
        "ok": not errors,
        "item_total": f"{total_calc:.2f}",
        "checked_at": now_iso(),
    }


# ---------------------------------------------------------------------------
# 拆票
# ---------------------------------------------------------------------------
def split_invoice(inv: ParsedInvoice, max_items: int = 20) -> list[ParsedInvoice]:
    """Split one invoice into multiple declarations when it has too many lines.

    The original invoice number stays in the header remark so the operator can
    trace each split declaration back to the source.
    """
    if max_items <= 0 or len(inv.items) <= max_items:
        return [inv]
    chunks = [inv.items[i:i + max_items] for i in range(0, len(inv.items), max_items)]
    out: list[ParsedInvoice] = []
    for idx, chunk in enumerate(chunks, start=1):
        header = dict(inv.header)
        header["external_ref"] = f"{inv.external_ref}-{idx:02d}"
        note = f"源发票{inv.external_ref}拆分第{idx}/{len(chunks)}票"
        header["remark"] = (header.get("remark") + "；" + note) if header.get("remark") else note
        renumbered = []
        for n, item in enumerate(chunk, start=1):
            data = item.to_dict()
            data["item_no"] = n
            renumbered.append(ParsedItem(**data))
        out.append(ParsedInvoice(
            external_ref=header["external_ref"],
            source_kind=inv.source_kind,
            source_file=inv.source_file,
            page_ref=f"{inv.page_ref} (分票{idx}/{len(chunks)})",
            header=header,
            items=renumbered,
            raw_warnings=list(inv.raw_warnings),
            sequence_no=inv.sequence_no + idx - 1,
        ))
    return out


def fingerprint(header: dict, items: list[ParsedItem]) -> str:
    payload = canonical_for_hash({
        "header": {k: v for k, v in header.items() if k not in ("external_ref", "remark")},
        "items": [
            {k: v for k, v in it.to_dict().items() if k != "item_no"}
            for it in items
        ],
    })
    return hashlib.sha256(json_dumps(payload).encode()).hexdigest()


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
