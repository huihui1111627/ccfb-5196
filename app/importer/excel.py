"""Excel invoice parsing.

Supported layouts:

1. **Wide / flat sheet** — one row per goods line, header values repeated or
   blank; a new invoice starts whenever the invoice-number cell changes.
2. **Header block + item table** — label/value pairs at the top of a sheet, items
   below.  Handled when the first row looks like a header block.

Both are common in real broker traffic.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from app.importer.base import (
    ParsedInvoice,
    ParsedItem,
    SourceParseError,
    match_header_label,
    match_item_label,
    normalize_header,
    normalize_item,
    split_invoice,
)
from common.formatting import clean_text

MAX_HEADER_SCAN_ROWS = 30


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return clean_text(value)


def _row_values(row) -> list[str]:
    return [_cell_text(v) for v in row]


def _nonempty(values) -> list[str]:
    return [v for v in values if v]


def _is_item_header(values: list[str]) -> bool:
    matched = sum(1 for v in values if v and match_item_label(v))
    return matched >= 3 and any("hs" in v.lower() or "HS" in v for v in values)


def parse_excel(path: str | Path) -> list[ParsedInvoice]:
    path = Path(path)
    try:
        wb = load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise SourceParseError(f"无法打开Excel文件：{exc}") from exc

    invoices: list[ParsedInvoice] = []
    try:
        for sheet in wb.worksheets:
            invoices.extend(_parse_sheet(sheet, path.name))
    finally:
        wb.close()

    if not invoices:
        raise SourceParseError("Excel中未找到可识别的发票数据（需包含发票号以及商品明细）")
    for idx, inv in enumerate(invoices):
        inv.sequence_no = idx
    return invoices


def _parse_sheet(sheet, filename: str) -> list[ParsedInvoice]:
    rows = []
    for row in sheet.iter_rows(values_only=True):
        values = _row_values(row)
        if not any(values):
            continue
        rows.append(values)
    if not rows:
        return []

    # Detect the header-block layout: first rows are label/value pairs.
    header_labels = 0
    for values in rows[:MAX_HEADER_SCAN_ROWS]:
        labels = [match_header_label(v) for v in values]
        if sum(1 for l in labels if l) >= 2:
            header_labels += 1
    header_block = header_labels >= 2

    if header_block and _find_item_header_row(rows) is not None:
        return [_parse_block_sheet(rows, filename, sheet.title)]

    if _find_item_header_row(rows) is not None:
        return _parse_wide_sheet(rows, filename, sheet.title)

    raise SourceParseError(f"工作表“{sheet.title}”缺少商品明细表头（需要HS编码/商品名称等列）")


def _find_item_header_row(rows) -> int | None:
    for i, values in enumerate(rows[:40]):
        matched = [v for v in values if v and match_item_label(v)]
        if len(matched) >= 3 and any(("hs" in v.lower()) or ("编码" in v) for v in matched):
            return i
    return None


def _column_map(header_values: list[str]) -> dict[int, str]:
    out = {}
    for i, v in enumerate(header_values):
        canon = match_item_label(v)
        if canon and canon not in out.values():
            out[i] = canon
    return out


# A column may be both an item column and a header column (币制 is the
# canonical example).  Item parsing wins for the line, but the header still
# reads the value.
PROMOTE_TO_HEADER = {"currency"}


def _parse_wide_sheet(rows, filename: str, sheet_title: str) -> list[ParsedInvoice]:
    """One row per item; header fields repeat on the first row of each invoice."""
    head_idx = _find_item_header_row(rows)
    assert head_idx is not None
    columns = _column_map(rows[head_idx])
    header_columns = {}
    for i, v in enumerate(rows[head_idx]):
        canon = match_header_label(v) if v else None
        if canon and (i not in columns or columns[i] in PROMOTE_TO_HEADER):
            header_columns[i] = canon
    # A separate header row usually precedes the item table in flat sheets.
    base_header: dict[str, str] = {}
    for values in rows[:head_idx]:
        for i, v in enumerate(values):
            canon = match_header_label(v)
            if canon:
                base_header[canon] = clean_text(values[i + 1]) if i + 1 < len(values) else ""

    invoices: list[ParsedInvoice] = []
    current_ref = None
    current_header: dict[str, str] = {}
    current_items: list[ParsedItem] = []

    def flush():
        nonlocal current_ref, current_header, current_items
        if current_ref and current_items:
            header = normalize_header({**base_header, **current_header,
                                       "external_ref": current_ref})
            invoices.append(ParsedInvoice(
                external_ref=header.get("external_ref") or current_ref,
                source_kind="excel", source_file=filename,
                page_ref=f"工作表{sheet_title} 行{start_row}",
                header=header,
                items=current_items,
            ))
        current_ref, current_header, current_items = None, {}, []

    start_row = head_idx + 2
    for offset, values in enumerate(rows[head_idx + 1:], start=1):
        item_raw = {columns[i]: values[i] for i in columns if i < len(values)}
        if not _nonempty(list(item_raw.values())) or not (item_raw.get("hs_code") or item_raw.get("goods_name")):
            continue
        # Header columns that repeat per row (flat layout).
        row_header = {}
        for i, canon in header_columns.items():
            if i < len(values) and clean_text(values[i]):
                row_header[canon] = clean_text(values[i])
        ref = row_header.get("external_ref") or current_ref
        if ref and ref != current_ref and current_items:
            flush()
            start_row = head_idx + 1 + offset
        if ref and ref != current_ref:
            current_ref = ref
            current_header = dict(row_header)
            start_row = head_idx + 1 + offset
        elif row_header:
            current_header.update(row_header)
        fallback_no = len(current_items) + 1
        current_items.append(normalize_item(item_raw, fallback_no,
                                             current_header.get("currency", "")))
    if current_items:
        flush()
    return invoices


def _parse_block_sheet(rows, filename: str, sheet_title: str) -> list[ParsedInvoice]:
    item_head = _find_item_header_row(rows)
    assert item_head is not None
    header_raw: dict[str, str] = {}
    for idx, values in enumerate(rows[:item_head]):
        for i, v in enumerate(values):
            canon = match_header_label(v)
            if not canon:
                continue
            rest = [clean_text(x) for x in values[i + 1:i + 3] if clean_text(x)]
            header_raw[canon] = rest[0] if rest else ""
    # Only one invoice per sheet in block layout (multi-invoice block sheets
    # are rare; operators split files instead).
    columns = _column_map(rows[item_head])
    items: list[ParsedItem] = []
    for values in rows[item_head + 1:]:
        raw = {columns[i]: values[i] for i in columns if i < len(values)}
        if not _nonempty(list(raw.values())):
            continue
        if not (raw.get("hs_code") or raw.get("goods_name")):
            continue
        items.append(normalize_item(raw, len(items) + 1, header_raw.get("currency", "")))
    if not items:
        return []
    header = normalize_header(header_raw)
    return [ParsedInvoice(
        external_ref=header.get("external_ref") or f"未编号-{sheet_title}",
        source_kind="excel", source_file=filename, page_ref=f"工作表{sheet_title}",
        header=header, items=items,
    )]
