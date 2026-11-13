"""Reconciliation: compare what the customs system holds with the source invoice."""
from __future__ import annotations

from typing import Any

from declarant.common import catalog, connect, hs_entry, normalize_hs_code, now_iso

HEADER_LABELS = {
    "invoice_no": "发票号",
    "port_code": "申报地海关",
    "transport_mode": "运输方式",
    "vessel_voyage": "运输工具/航次",
    "trade_country": "贸易国",
    "dest_country": "运抵国",
    "currency": "币制",
    "exchange_rate": "汇率",
    "package_count": "包装件数",
    "packing_type": "包装种类",
    "gross_weight": "毛重",
    "net_weight": "净重",
    "contract_no": "合同号",
    "trade_terms": "成交方式",
    "freight": "运费",
    "insurance": "保费",
    "other_charges": "杂费",
    "consignee": "境内收发货人",
    "consignor": "境外收发货人",
    "processing_ratio": "加工贸易比例",
    "bonded_register_no": "加工贸易手册号",
    "warehouse_no": "保税仓库号",
    "storage_days": "仓储期限",
    "ecommerce_order_no": "电商订单号",
    "ecommerce_platform": "电商平台",
    "guarantee_no": "担保编号",
    "guarantee_amount": "担保金额",
    "original_decl_no": "原报关单号",
    "return_reason": "退运原因",
    "lease_term_months": "租赁期限",
    "trade_mode": "贸易方式",
}

NUMERIC_FIELDS = {"exchange_rate", "package_count", "gross_weight", "net_weight", "freight",
                  "insurance", "other_charges", "processing_ratio", "storage_days",
                  "guarantee_amount", "lease_term_months", "quantity", "legal_quantity",
                  "second_quantity", "unit_price", "total_price", "quantity_unit",
                  "legal_quantity_unit"}

ITEM_LABELS = {
    "hs_code": "HS编码", "goods_name": "商品名称", "specification": "规格型号",
    "quantity": "成交数量", "legal_quantity": "法定数量", "unit_price": "单价",
    "total_price": "总价", "origin_country": "原产国", "unit": "成交单位",
    "legal_unit": "法定单位", "brand": "品牌", "model": "型号",
}


def _norm_number(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return round(float(str(v).replace(",", "").strip()), 4)
    except (TypeError, ValueError):
        return None


def _cmp(key: str, source: Any, actual: Any) -> bool:
    """True when the two values are considered identical."""
    if key in NUMERIC_FIELDS:
        a, b = _norm_number(source), _norm_number(actual)
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return abs(a - b) < 0.011
    s = str(source if source is not None else "").strip()
    t = str(actual if actual is not None else "").strip()
    if key == "hs_code":
        return normalize_hs_code(s) == normalize_hs_code(t)
    if key == "origin_country":
        return s.upper() == t.upper()
    return s == t


def build_source_snapshot(decl: dict[str, Any]) -> dict[str, Any]:
    header = {k: decl.get(k) for k in HEADER_LABELS}
    items = []
    for it in decl.get("items", []):
        row = {k: it.get(k) for k in ITEM_LABELS if k not in ("unit", "legal_unit")}
        row["unit"] = it.get("unit")
        row["legal_unit"] = it.get("legal_unit")
        row["elements"] = it.get("elements") or {}
        items.append(row)
    return {"header": header, "items": items}


def compare(source: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a flat list of field comparisons.

    Each entry: field_path, label, source_value, system_value, match(bool)
    """
    rows: list[dict[str, Any]] = []
    src_header = source.get("header", {})
    act_header = actual.get("header", {})
    for key, label in HEADER_LABELS.items():
        if key == "trade_mode":
            continue
        src_val = src_header.get(key)
        if src_val in (None, "") and key not in act_header:
            continue
        act_val = act_header.get(key)
        rows.append({"field_path": f"header.{key}", "label": label,
                     "source_value": _fmt(src_val), "system_value": _fmt(act_val),
                     "match": _cmp(key, src_val, act_val)})
    src_mode = str(src_header.get("trade_mode") or "")
    act_mode = str(act_header.get("trade_mode") or "")
    rows.append({"field_path": "header.trade_mode", "label": "贸易方式",
                 "source_value": src_mode, "system_value": act_mode,
                 "match": src_mode == act_mode})

    src_items = source.get("items", [])
    act_items = actual.get("items", [])
    max_len = max(len(src_items), len(act_items))
    for idx in range(max_len):
        seq = idx + 1
        s = src_items[idx] if idx < len(src_items) else {}
        a = act_items[idx] if idx < len(act_items) else {}
        for key, label in ITEM_LABELS.items():
            if key in ("unit", "legal_unit"):
                continue
            src_val = s.get(key)
            if src_val in (None, "") and a.get(key) in (None, ""):
                continue
            act_val = a.get(key)
            if key in ("hs_code", "quantity", "total_price", "goods_name", "origin_country",
                       "unit_price", "legal_quantity"):
                rows.append({"field_path": f"items[{seq}].{key}", "label": f"第{seq}项 {label}",
                             "source_value": _fmt(src_val), "system_value": _fmt(act_val),
                             "match": _cmp(key, src_val, act_val)})
        src_elements = s.get("elements") or {}
        act_elements = a.get("elements") or {}
        for el_key, el_val in src_elements.items():
            entry = hs_entry(normalize_hs_code(s.get("hs_code", "")))
            el_label = el_key
            if entry:
                spec = next((x for x in entry["elements"] if x["key"] == el_key), None)
                if spec:
                    el_label = spec["label"]
            act_val = act_elements.get(el_key, "")
            rows.append({"field_path": f"items[{seq}].elements.{el_key}",
                         "label": f"第{seq}项 要素[{el_label}]",
                         "source_value": _fmt(el_val), "system_value": _fmt(act_val),
                         "match": str(el_val).strip() == str(act_val).strip()})
    return rows


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def save_comparison(decl_id: int, mock_decl_id: str, rows: list[dict[str, Any]],
                    snapshot: dict[str, Any]) -> dict[str, Any]:
    conn = connect()
    try:
        ts = now_iso()
        conn.execute("DELETE FROM reconciliation WHERE declaration_id=?", (decl_id,))
        mismatched = 0
        for r in rows:
            if r["match"]:
                continue
            mismatched += 1
            conn.execute(
                "INSERT INTO reconciliation(declaration_id,field_path,source_value,system_value,"
                "severity,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (decl_id, r["field_path"], r["source_value"], r["system_value"],
                 "error", "open", ts))
        conn.execute(
            "INSERT INTO reconciliation_run(declaration_id,mock_decl_id,total_fields,mismatched,"
            "result,snapshot,created_at) VALUES(?,?,?,?,?,?,?)",
            (decl_id, mock_decl_id, len(rows), mismatched,
             "ok" if mismatched == 0 else "mismatch",
             __import__("json").dumps(snapshot, ensure_ascii=False), ts))
        conn.commit()
        return {"total": len(rows), "mismatched": mismatched,
                "rows": rows}
    finally:
        conn.close()


def latest_diff(decl_id: int) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM reconciliation WHERE declaration_id=? ORDER BY field_path",
            (decl_id,)).fetchall()]
    finally:
        conn.close()
