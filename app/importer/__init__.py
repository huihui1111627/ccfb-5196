"""Import orchestration: persist batches/documents/items from parsed sources."""

from __future__ import annotations

import shutil
from pathlib import Path

from app.importer import excel as excel_parser, pdf as pdf_parser
from app.db import Database, json_dumps, now_iso
from app.importer.base import (
    ParsedInvoice,
    file_sha256,
    fingerprint,
    split_invoice,
    validate_invoice,
)
from app.services.state import set_document_state, log_event
from app.config import DATA_DIR
from common.formatting import clean_text

UPLOAD_DIR = Path(DATA_DIR) / "sources"


def import_source_file(db: Database, src_path: str | Path, filename: str | None = None,
                       note: str | None = None, max_items_per_ticket: int = 20) -> dict:
    """Parse, validate and persist one Excel/PDF.  Returns an import summary."""
    src_path = Path(src_path)
    if not src_path.exists():
        raise FileNotFoundError(src_path)
    filename = filename or src_path.name
    suffix = src_path.suffix.lower()

    if suffix in (".xlsx", ".xlsm", ".xls"):
        invoices = excel_parser.parse_excel(src_path)
        kind = "excel"
    elif suffix == ".pdf":
        invoices = pdf_parser.parse_pdf(src_path)
        kind = "pdf"
    else:
        raise ValueError(f"不支持的文件类型：{suffix}（请上传Excel或PDF）")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    digest = file_sha256(src_path)
    stored = UPLOAD_DIR / f"{digest[:12]}_{filename}"
    if not stored.exists():
        shutil.copy2(src_path, stored)

    split: list[ParsedInvoice] = []
    for inv in invoices:
        split.extend(split_invoice(inv, max_items=max_items_per_ticket))

    batch_id = None
    created, skipped, failed = [], [], []
    with db.tx(immediate=True):
        batch_id = db.execute(
            "INSERT INTO batches(name, source_filename, source_kind, source_sha256, source_path, "
            "doc_count, imported_at, note) VALUES(?,?,?,?,?,?,?,?)",
            (filename, filename, kind, digest, str(stored), len(split), now_iso(), note or ""),
        )
        for inv in split:
            validation = validate_invoice(inv.header, inv.items)
            fp = fingerprint(inv.header, inv.items)
            exists = db.one(
                "SELECT id FROM documents WHERE external_ref=? AND fingerprint=? AND "
                "status NOT IN ('cancelled')",
                (inv.external_ref, fp),
            )
            if exists:
                skipped.append({"external_ref": inv.external_ref,
                                "reason": "同一发票号且内容一致的单据已存在",
                                "document_id": exists["id"]})
                continue
            try:
                doc_id = _persist_document(db, batch_id, inv, validation, fp)
            except Exception as exc:  # noqa: BLE001
                failed.append({"external_ref": inv.external_ref, "error": str(exc)})
                continue
            created.append({"document_id": doc_id, "external_ref": inv.external_ref,
                            "items": len(inv.items),
                            "ok": validation["ok"],
                            "errors": len(validation["errors"]),
                            "warnings": len(validation["warnings"])})

        state = "ready" if created else "empty"
        if any(not c["ok"] for c in created):
            state = "needs_fix"
            for c in created:
                if not c["ok"]:
                    set_document_state(db, c["document_id"], "needs_fix")
        else:
            for c in created:
                set_document_state(db, c["document_id"], "ready")
        log_event(db, None, "import", state,
                  f"{filename}: 新增{len(created)}票，跳过{len(skipped)}，失败{len(failed)}")

    return {
        "batch_id": batch_id,
        "filename": filename,
        "kind": kind,
        "invoice_count": len(invoices),
        "split_count": len(split),
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "state": state,
    }


def _persist_document(db: Database, batch_id: int, inv: ParsedInvoice,
                      validation: dict, fp: str) -> int:
    items = [it.to_dict() for it in inv.items]
    doc_id = db.execute(
        "INSERT INTO documents(batch_id, external_ref, source_kind, source_file, page_ref, "
        "sequence_no, status, header_json, items_json, validation_json, validation_blocking, "
        "fingerprint, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (batch_id, inv.external_ref, inv.source_kind, inv.source_file, inv.page_ref,
         inv.sequence_no, "ready", json_dumps(inv.header), json_dumps(items),
         json_dumps(validation), 1 if validation["errors"] else 0, fp, now_iso(), now_iso()),
    )
    for it in inv.items:
        db.execute(
            "INSERT INTO document_items(document_id, item_no, hs_code, goods_name, spec_model, "
            "quantity, unit, unit_price, total_price, currency, origin_country, "
            "destination_country, elements_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, it.item_no, it.hs_code, it.goods_name, it.spec_model, it.quantity,
             it.unit, it.unit_price, it.total_price, it.currency, it.origin_country,
             it.destination_country, json_dumps(it.elements)),
        )
    return doc_id


def update_document_items(db: Database, doc_id: int, changes: list[dict]) -> dict:
    """Apply operator corrections to a document, re-validate, recompute fingerprint."""
    doc = db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc:
        raise KeyError(doc_id)
    import json

    header = json.loads(doc["header_json"])
    items = json.loads(doc["items_json"])
    by_no = {int(it["item_no"]): it for it in items}
    for change in changes:
        item_no = int(change["item_no"])
        target = by_no.get(item_no)
        if target is None:
            raise ValueError(f"项号{item_no}不存在")
        for key, value in (change.get("fields") or {}).items():
            if key == "elements":
                target["elements"] = dict(value or {})
            elif key in target:
                target[key] = clean_text(value)
    for it in items:
        try:
            from decimal import Decimal

            q = Decimal(str(it["quantity"]))
            p = Decimal(str(it["unit_price"]))
            it["total_price"] = f"{(q * p).quantize(Decimal('0.01')):.2f}"
        except Exception:  # noqa: BLE001
            pass
    from app.importer.base import ParsedItem, validate_invoice, fingerprint as fp_fn

    parsed_items = []
    for it in items:
        parsed_items.append(ParsedItem(
            item_no=int(it["item_no"]), hs_code=it.get("hs_code", ""),
            goods_name=it.get("goods_name", ""), spec_model=it.get("spec_model", ""),
            quantity=it.get("quantity", ""), unit=it.get("unit", ""),
            unit_price=it.get("unit_price", ""), total_price=it.get("total_price", ""),
            currency=it.get("currency", ""), origin_country=it.get("origin_country", ""),
            destination_country=it.get("destination_country", ""),
            elements=dict(it.get("elements") or {}),
        ))
    validation = validate_invoice(header, parsed_items)
    fp = fp_fn(header, parsed_items)
    db.execute(
        "UPDATE documents SET items_json=?, validation_json=?, validation_blocking=?, "
        "fingerprint=?, updated_at=? WHERE id=?",
        (json_dumps(items), json_dumps(validation), 1 if validation["errors"] else 0,
         fp, now_iso(), doc_id),
    )
    return {"document_id": doc_id, "validation": validation, "fingerprint": fp}


def update_document_header(db: Database, doc_id: int, header_changes: dict) -> dict:
    import json
    from app.importer.base import ParsedItem, validate_invoice, fingerprint as fp_fn

    doc = db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc:
        raise KeyError(doc_id)
    header = json.loads(doc["header_json"])
    items = json.loads(doc["items_json"])
    for key, value in header_changes.items():
        if key == "external_ref":
            continue
        if key in header:
            header[key] = clean_text(value)
    parsed_items = [ParsedItem(
        item_no=int(it["item_no"]), hs_code=it.get("hs_code", ""),
        goods_name=it.get("goods_name", ""), spec_model=it.get("spec_model", ""),
        quantity=it.get("quantity", ""), unit=it.get("unit", ""),
        unit_price=it.get("unit_price", ""), total_price=it.get("total_price", ""),
        currency=it.get("currency", ""), origin_country=it.get("origin_country", ""),
        destination_country=it.get("destination_country", ""),
        elements=dict(it.get("elements") or {}),
    ) for it in items]
    validation = validate_invoice(header, parsed_items)
    fp = fp_fn(header, parsed_items)
    db.execute(
        "UPDATE documents SET header_json=?, validation_json=?, validation_blocking=?, "
        "fingerprint=?, updated_at=? WHERE id=?",
        (json_dumps(header), json_dumps(validation), 1 if validation["errors"] else 0,
         fp, now_iso(), doc_id),
    )
    return {"document_id": doc_id, "validation": validation}
