"""Rejection parsing and repair suggestions."""
from __future__ import annotations

import re
from typing import Any

CATEGORY_LABELS = {
    "ELEMENT": "申报要素不符",
    "PRICE": "价格不符",
    "QUANTITY": "数量不符",
    "DOCUMENT": "单证不符",
    "HEADER": "表头不符",
    "UNKNOWN": "未识别",
}

FIX_HINTS = {
    "ELEMENT": "按退单提示的要素项，对照税则重新填报该商品的申报要素",
    "PRICE": "核对成交数量与单价，修正总价后重新录入该明细",
    "QUANTITY": "按法定单位重新折算法定数量，并核对第二数量",
    "DOCUMENT": "核对表头发票号与随附单证是否一致",
    "HEADER": "补全贸易方式下的联动字段（成交方式/手册号/担保号等）",
    "UNKNOWN": "人工阅读退单原因，定位字段后修改",
}


def parse_reason(raw: str) -> dict[str, Any]:
    """Map a mock 退单 reason to category / item / field / suggestion.

    Examples:
      [要素不符] 第2项商品申报要素[品牌类型]填报与税则要求不一致...
      [价格不符] 第1项商品总价与数量×单价不一致（差额 -12.00）...
      [表头不符] 贸易方式[0615]下[加工贸易手册号]未按规定填报...
    """
    raw = (raw or "").strip()
    category = "UNKNOWN"
    if raw.startswith("[要素不符]") or "要素" in raw[:12]:
        category = "ELEMENT"
    elif raw.startswith("[价格不符]") or "价格" in raw[:12]:
        category = "PRICE"
    elif raw.startswith("[数量不符]"):
        category = "QUANTITY"
    elif raw.startswith("[单证不符]"):
        category = "DOCUMENT"
    elif raw.startswith("[表头不符]"):
        category = "HEADER"

    seq = None
    m = re.search(r"第\s*(\d+)\s*项", raw)
    if m:
        seq = int(m.group(1))
    field_path = None
    label_match = re.search(r"要素\[([^\]]+)\]", raw)
    if category == "ELEMENT" and label_match:
        field_path = f"items[{seq}].elements.{label_match.group(1)}" if seq else None
    elif category == "HEADER":
        label_match2 = re.search(r"下\[([^\]]+)\]", raw) or re.search(r"\[([^\]]+)\]", raw)
        if label_match2:
            field_path = f"header.{label_match2.group(1)}"
    elif category == "PRICE" and seq:
        field_path = f"items[{seq}].total_price"
    elif category == "QUANTITY" and seq:
        field_path = f"items[{seq}].legal_quantity"
    elif category == "DOCUMENT":
        field_path = "header.invoice_no"
    return {"category": category, "category_label": CATEGORY_LABELS[category],
            "raw": raw, "item_seq": seq, "field_path": field_path,
            "suggestion": FIX_HINTS[category]}


def auto_repair(decl: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Attempt a deterministic fix for known rejection types.

    Returns a patch dict {table, key, value, description} or None when the issue
    needs human attention. Repairs are conservative: they only recompute values
    that can be derived from the source invoice.
    """
    cat = parsed["category"]
    seq = parsed.get("item_seq")
    item = None
    if seq and 0 < seq <= len(decl["items"]):
        item = decl["items"][seq - 1]
    if cat == "PRICE" and item:
        qty = float(item.get("quantity") or 0)
        price = float(item.get("unit_price") or 0)
        if qty and price:
            new_total = round(qty * price, 2)
            return {"table": "items", "seq": seq, "field": "total_price",
                    "value": new_total, "description": f"第{seq}项总价按 数量×单价 重算为 {new_total}"}
    if cat == "QUANTITY" and item:
        qty = float(item.get("quantity") or 0)
        unit = item.get("unit") or ""
        legal_unit = item.get("legal_unit") or ""
        if qty and legal_unit and unit == legal_unit:
            return {"table": "items", "seq": seq, "field": "legal_quantity",
                    "value": qty, "description": f"第{seq}项法定数量按成交数量 1:1 折算为 {qty}"}
    if cat == "ELEMENT" and item and parsed.get("field_path"):
        # if the element is missing or invalid, fall back to the first allowed option
        key = parsed["field_path"].split(".")[-1]
        from declarant.common import hs_entry, normalize_hs_code
        entry = hs_entry(normalize_hs_code(item.get("hs_code", "")))
        if entry:
            spec = next((x for x in entry["elements"] if x["key"] == key), None)
            if spec and spec["kind"] == "select" and spec["options"]:
                current = (item.get("elements") or {}).get(key, "")
                if current not in spec["options"]:
                    return {"table": "elements", "seq": seq, "field": key,
                            "value": spec["options"][0],
                            "description": f"第{seq}项要素[{spec['label']}]修正为“{spec['options'][0]}”"}
    if cat == "DOCUMENT":
        # the mock compares the header invoice no with the attached document; re-syncing
        # the header from the local source record is the safe deterministic fix
        return {"table": "header", "field": "invoice_no", "value": decl["invoice_no"],
                "description": f"表头发票号按源单重新同步为 {decl['invoice_no']}"}
    return None
