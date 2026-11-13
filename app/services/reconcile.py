"""Read-back verification: compare the customs system's stored declaration
against the source invoice, field by field.

Mismatches are flagged (and surfaced in the console in red), never auto-corrected.
"""

from __future__ import annotations

import json

from common import catalog
from common.formatting import clean_text, parse_elements, values_equal

# Labels used in the UI for header fields we compare.
HEADER_COMPARE = [
    "trade_mode", "declaration_type", "port", "consignor", "consignee",
    "destination_country", "currency",
]
ITEM_COMPARE = [
    "hs_code", "goods_name", "spec_model", "quantity", "unit", "unit_price",
    "total_price", "origin_country", "destination_country",
]


def _label(field: str) -> str:
    return catalog.DECLARATION_FIELD_LABELS.get(field, catalog.ITEM_FIELD_LABELS.get(field, field))


def compare_document(source_header: dict, source_items: list[dict],
                     system_decl: dict) -> dict:
    """Return {"matched": bool, "differences": [...], "checked_fields": n}."""
    system_header = (system_decl or {}).get("header") or {}
    system_items_raw = (system_decl or {}).get("items") or {}
    system_items = {}
    for key, value in system_items_raw.items():
        try:
            system_items[int(key)] = value
        except (TypeError, ValueError):
            continue

    differences: list[dict] = []
    checks = 0

    for field in HEADER_COMPARE:
        checks += 1
        expected = source_header.get(field, "")
        actual = system_header.get(field, "")
        if not values_equal(field, expected, actual):
            differences.append({
                "scope": "header", "field": field, "label": _label(field),
                "expected": clean_text(expected), "actual": clean_text(actual),
            })
    # Dynamic fields required by the trade mode.
    mode = source_header.get("trade_mode", "")
    for field in catalog.required_fields(mode):
        checks += 1
        expected = source_header.get(field, "")
        actual = system_header.get(field, "")
        if values_equal(field, expected, actual):
            continue
        if not clean_text(expected) and not clean_text(actual):
            continue
        differences.append({
            "scope": "header", "field": field, "label": _label(field),
            "expected": clean_text(expected), "actual": clean_text(actual),
        })

    for item in source_items:
        item_no = int(item["item_no"])
        system_item = system_items.get(item_no)
        if system_item is None:
            differences.append({
                "scope": "item", "field": "item_no", "label": "项号",
                "item_no": item_no, "expected": str(item_no), "actual": "明细缺失",
                "severity": "missing",
            })
            continue
        for field in ITEM_COMPARE:
            checks += 1
            expected = item.get(field, "")
            actual = system_item.get(field, "")
            if not values_equal(field, expected, actual):
                differences.append({
                    "scope": "item", "field": field, "label": _label(field),
                    "item_no": item_no, "expected": clean_text(expected),
                    "actual": clean_text(actual),
                })
        checks += 1
        src_elements = parse_elements(item.get("elements") or {})
        sys_elements = parse_elements(system_item.get("elements") or {})
        if src_elements != sys_elements:
            differing = sorted({k for k in src_elements if src_elements.get(k) != sys_elements.get(k)}
                               | {k for k in sys_elements if src_elements.get(k) != sys_elements.get(k)})
            differences.append({
                "scope": "item", "field": "elements", "label": "申报要素",
                "item_no": item_no,
                "expected": "；".join(f"{k}={v}" for k, v in src_elements.items()),
                "actual": "；".join(f"{k}={v}" for k, v in sys_elements.items()),
                "differing_keys": differing,
            })

    extra_items = sorted(set(system_items) - {int(i["item_no"]) for i in source_items})
    for extra in extra_items:
        differences.append({
            "scope": "item", "field": "item_no", "label": "多余明细",
            "item_no": extra, "expected": "—", "actual": f"项号{extra}",
        })

    return {
        "matched": not differences,
        "differences": differences,
        "checked_fields": checks,
        "system_declaration_no": (system_decl or {}).get("declaration_no"),
        "item_count_source": len(source_items),
        "item_count_system": len(system_items),
    }


def verify_document(db, doc_id: int, portal, run_id: int | None = None) -> dict:
    """Read back the declaration and persist the comparison."""
    doc = db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc:
        raise KeyError(doc_id)
    sub = db.one("SELECT * FROM submissions WHERE doc_id=?", (doc_id,))
    if not sub or not sub["declaration_no"]:
        raise ValueError("该票尚未取得报关单号，无法回读核对")
    header = json.loads(doc["header_json"])
    items = json.loads(doc["items_json"])

    decl = portal.read_declaration(sub["declaration_no"])
    if not decl:
        result = {
            "matched": False, "differences": [{
                "scope": "header", "field": "declaration_no", "label": "报关单号",
                "expected": sub["declaration_no"], "actual": "系统中未找到该申报单",
            }], "checked_fields": 0,
        }
    else:
        result = compare_document(header, items, decl)
    db.execute(
        "UPDATE documents SET compare_json=?, updated_at=? WHERE id=?",
        (json.dumps(result, ensure_ascii=False), _now(), doc_id),
    )
    # The caller (runner or console) records the step; logging here would
    # double-count verify events in the daily statistics.

    return result


def _now() -> str:
    from app.db import now_iso
    return now_iso()
