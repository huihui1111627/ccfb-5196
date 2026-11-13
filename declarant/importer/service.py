"""Import pipeline: parse files -> validate -> split into declarations -> queue.

Duplicate protection starts here: each invoice gets a stable fingerprint
(invoice_no + trade_mode + normalised item lines). A second import of the
same invoice creates no new declaration and no new mock record.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from declarant.common import (add_to_daily, connect, ensure_dirs, now_iso, severity_counts,
                              trade_mode_fields, validate_declaration, data_dir)

SOURCE_FIELDS = ("invoice_no", "contract_no", "trade_mode", "port_code", "transport_mode",
                 "vessel_voyage", "trade_country", "origin_country", "dest_country",
                 "trade_terms", "currency", "exchange_rate", "freight", "insurance",
                 "other_charges", "package_count", "packing_type", "gross_weight",
                 "net_weight", "consignor", "consignee", "total_amount",
                 "processing_ratio", "bonded_register_no", "warehouse_no", "storage_days",
                 "ecommerce_order_no", "ecommerce_platform", "guarantee_no",
                 "guarantee_amount", "original_decl_no", "return_reason", "lease_term_months")

ITEM_FIELDS = ("hs_code", "goods_name", "specification", "brand", "model", "quantity",
               "unit", "legal_quantity", "legal_unit", "second_quantity", "second_unit",
               "unit_price", "total_price", "origin_country")


def fingerprint(invoice: dict[str, Any], items: list[dict[str, Any]]) -> str:
    payload = {
        "invoice_no": str(invoice.get("invoice_no", "")).strip().upper(),
        "trade_mode": str(invoice.get("trade_mode", "")).strip(),
        "items": [
            {k: (str(it.get(k, "")).strip() if it.get(k) is not None else "")
             for k in ("hs_code", "goods_name", "quantity", "total_price")}
            for it in items
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()


def source_snapshot(invoice: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    snap = {k: invoice.get(k) for k in SOURCE_FIELDS if invoice.get(k) is not None}
    snap["items"] = [{k: it.get(k) for k in ITEM_FIELDS} | {"elements": it.get("elements", {})}
                     for it in items]
    return snap


def save_upload(filename: str, content: bytes) -> Path:
    ensure_dirs()
    safe = Path(filename).name
    target = data_dir() / "uploads" / f"{now_iso().replace(' ', '_').replace(':', '')}_{safe}"
    target.write_bytes(content)
    return target


def import_file(path: Path, original_name: str | None = None) -> dict[str, Any]:
    from declarant.importer.parsers import parse_file

    ensure_dirs()
    conn = connect()
    try:
        batch_id = _create_batch(conn, original_name or path.name,
                                 "pdf" if path.suffix.lower() == ".pdf" else "excel")
        try:
            parsed = parse_file(path)
        except Exception as exc:  # noqa: BLE001 - report parse failures to the user
            _finish_batch(conn, batch_id, "failed", report={"error": f"{type(exc).__name__}: {exc}"})
            return {"ok": False, "batch_id": batch_id, "error": str(exc)}
        report: dict[str, Any] = {"invoices": [], "summary": {}}
        imported = skipped = errors = 0
        for p in parsed:
            inv = p.invoice
            items = p.items
            fp = fingerprint(inv, items)
            result = _import_one(conn, batch_id, inv, items, p, fp)
            report["invoices"].append(result)
            if result["status"] == "validated":
                imported += 1
            elif result["status"] == "hold":
                imported += 1
                errors += 1
            elif result["status"] == "duplicate":
                skipped += 1
            else:
                errors += 1
        report["summary"] = {"parsed": len(parsed), "imported": imported,
                             "duplicate": skipped, "invalid": errors}
        _finish_batch(conn, batch_id, "done", report=report,
                      total=len(parsed), imported=imported, skipped=skipped, errors=errors,
                      sheets=len({p.sheet for p in parsed if p.sheet}))
        conn.commit()
        return {"ok": True, "batch_id": batch_id, "report": report,
                "imported": imported, "duplicate": skipped, "invalid": errors}
    finally:
        conn.close()


def _import_one(conn: sqlite3.Connection, batch_id: int, inv: dict[str, Any],
                items: list[dict[str, Any]], parsed, fp: str) -> dict[str, Any]:
    dup = conn.execute("SELECT id,status,mock_decl_id FROM declarations WHERE fingerprint=?",
                       (fp,)).fetchone()
    if dup:
        return {"invoice_no": inv.get("invoice_no"), "fingerprint": fp, "status": "duplicate",
                "declaration_id": dup["id"], "existing_status": dup["status"],
                "message": "与已导入的发票重复，未重复建单"}
    field_map = {k: k for k in SOURCE_FIELDS if inv.get(k) is not None}
    linked = {f["name"]: inv.get(f["name"]) for f in trade_mode_fields(inv.get("trade_mode", ""))
              if inv.get(f["name"]) not in (None, "")}
    issues = validate_declaration(inv, items)
    counts = severity_counts(issues)
    status = "hold" if counts["error"] else "validated"
    ts = now_iso()
    cur = conn.execute(
        """INSERT INTO declarations(batch_id,invoice_no,contract_no,trade_mode,nature,duty_mode,
           customs_code,port_code,transport_mode,vessel_voyage,trade_country,origin_country,
           dest_country,trade_terms,currency,exchange_rate,total_amount,freight,insurance,
           other_charges,package_count,packing_type,gross_weight,net_weight,consignor,consignee,
           source_file,source_kind,source_page,fingerprint,field_map,linked_json,status,
           error_reason,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (batch_id, inv.get("invoice_no"), inv.get("contract_no"), inv.get("trade_mode"),
         inv.get("nature"), inv.get("duty_mode"), "", inv.get("port_code"),
         inv.get("transport_mode"), inv.get("vessel_voyage"), inv.get("trade_country"),
         inv.get("origin_country"), inv.get("dest_country"), inv.get("trade_terms"),
         inv.get("currency"), inv.get("exchange_rate"), inv.get("total_amount"),
         float(inv.get("freight") or 0), float(inv.get("insurance") or 0),
         float(inv.get("other_charges") or 0), int(inv.get("package_count") or 0),
         inv.get("packing_type"), float(inv.get("gross_weight") or 0),
         float(inv.get("net_weight") or 0), inv.get("consignor"), inv.get("consignee"),
         parsed.source_file, parsed.source_kind, parsed.source_page, fp,
         json.dumps(field_map, ensure_ascii=False),
         json.dumps({k: v for k, v in linked.items()}, ensure_ascii=False), status,
         json.dumps([i.as_dict() for i in issues], ensure_ascii=False) if issues else None,
         ts, ts))
    decl_id = cur.lastrowid
    for it in items:
        conn.execute(
            """INSERT INTO declaration_items(declaration_id,seq_no,hs_code,goods_name,specification,
               brand,model,quantity,unit,legal_quantity,legal_unit,second_quantity,second_unit,
               unit_price,total_price,origin_country,currency,elements,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (decl_id, it.get("seq_no") or 0, it.get("hs_code"), it.get("goods_name"),
             it.get("specification"), it.get("brand"), it.get("model"),
             float(it.get("quantity") or 0), it.get("unit"),
             it.get("legal_quantity"), it.get("legal_unit"), it.get("second_quantity"),
             it.get("second_unit"), float(it.get("unit_price") or 0),
             float(it.get("total_price") or 0), it.get("origin_country"),
             inv.get("currency"), json.dumps(it.get("elements") or {}, ensure_ascii=False), ts, ts))
    if status == "validated":
        conn.execute("INSERT INTO jobs(declaration_id,kind,status,created_at,updated_at) "
                     "VALUES(?,?,?,?,?) ON CONFLICT(declaration_id,kind) DO NOTHING",
                     (decl_id, "record", "pending", ts, ts))
    add_to_daily(conn, "imported_count")
    conn.execute("INSERT INTO audit_log(action,fingerprint,declaration_id,detail,created_at)"
                 " VALUES(?,?,?,?,?)", ("import", fp, decl_id,
                                        json.dumps({"status": status, "source": parsed.source_file},
                                                   ensure_ascii=False), ts))
    conn.commit()
    return {"invoice_no": inv.get("invoice_no"), "fingerprint": fp, "status": status,
            "declaration_id": decl_id, "issues": [i.as_dict() for i in issues],
            "severity": counts, "items": len(items), "source": parsed.source_file,
            "sheet": parsed.sheet, "page": parsed.source_page}


def _create_batch(conn: sqlite3.Connection, filename: str, kind: str) -> int:
    cur = conn.execute("INSERT INTO import_batches(filename,kind,status,created_at) "
                       "VALUES(?,?,?,?)", (filename, kind, "processing", now_iso()))
    conn.commit()
    return cur.lastrowid


def _finish_batch(conn: sqlite3.Connection, batch_id: int, status: str, *,
                  report: dict[str, Any], total: int = 0, imported: int = 0,
                  skipped: int = 0, errors: int = 0, sheets: int = 0) -> None:
    conn.execute(
        "UPDATE import_batches SET status=?, total_rows=?, imported_rows=?, skipped_rows=?,"
        " error_rows=?, split_sheets=?, report=?, finished_at=? WHERE id=?",
        (status, total, imported, skipped, errors, sheets,
         json.dumps(report, ensure_ascii=False), now_iso(), batch_id))
    conn.commit()


def revalidate(decl_id: int) -> list[dict[str, str]]:
    """Re-run validation after manual edits; promote hold -> validated."""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM declarations WHERE id=?", (decl_id,)).fetchone()
        if not row:
            return []
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM declaration_items WHERE declaration_id=? ORDER BY seq_no",
            (decl_id,)).fetchall()]
        for it in items:
            if isinstance(it.get("elements"), str):
                it["elements"] = json.loads(it["elements"] or "{}")
        decl = dict(row)
        try:
            decl.update(json.loads(decl.get("linked_json") or "{}"))
        except json.JSONDecodeError:
            pass
        issues = validate_declaration(decl, items)
        counts = severity_counts(issues)
        if counts["error"]:
            # blocking issues always force a hold, even if the record was previously
            # validated or abnormal — better to stop than to type bad data into customs
            conn.execute("UPDATE declarations SET status='hold', error_reason=?, updated_at=? "
                         "WHERE id=? AND status NOT IN ('approved','submitted','recording')",
                         (json.dumps([i.as_dict() for i in issues], ensure_ascii=False),
                          now_iso(), decl_id))
        else:
            conn.execute("UPDATE declarations SET error_reason=?, updated_at=? WHERE id=?",
                         (json.dumps([i.as_dict() for i in issues], ensure_ascii=False) if issues else None,
                          now_iso(), decl_id))
            if row["status"] == "hold":
                conn.execute("UPDATE declarations SET status='validated', updated_at=? WHERE id=?",
                             (now_iso(), decl_id))
                conn.execute("INSERT INTO jobs(declaration_id,kind,status,created_at,updated_at)"
                             " VALUES(?,?,?,?,?) ON CONFLICT(declaration_id,kind) DO NOTHING",
                             (decl_id, "record", "pending", now_iso(), now_iso()))
        conn.commit()
        return [i.as_dict() for i in issues]
    finally:
        conn.close()


def load_declaration(decl_id: int) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM declarations WHERE id=?", (decl_id,)).fetchone()
        if not row:
            return None
        decl = dict(row)
        items = []
        for r in conn.execute("SELECT * FROM declaration_items WHERE declaration_id=? "
                              "ORDER BY seq_no", (decl_id,)).fetchall():
            d = dict(r)
            try:
                d["elements"] = json.loads(d.get("elements") or "{}")
            except json.JSONDecodeError:
                d["elements"] = {}
            items.append(d)
        decl["items"] = items
        try:
            decl["issues"] = json.loads(decl["error_reason"] or "[]")
        except json.JSONDecodeError:
            decl["issues"] = []
        try:
            decl["field_map"] = json.loads(decl.get("field_map") or "{}")
        except json.JSONDecodeError:
            decl["field_map"] = {}
        try:
            decl["linked"] = json.loads(decl.get("linked_json") or "{}")
        except json.JSONDecodeError:
            decl["linked"] = {}
        decl.update(decl["linked"])
        # the customs system keys declarations on the client reference (= invoice no.)
        decl["client_ref"] = decl.get("invoice_no") or f"DECL-{decl['id']}"
        return decl
    finally:
        conn.close()


def batch_report(batch_id: int) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["report"] = json.loads(d.get("report") or "{}")
        except json.JSONDecodeError:
            d["report"] = {}
        return d
    finally:
        conn.close()
