"""PDF commercial-invoice parsing.

PDF text extraction is line-oriented: a single visual table row often comes
back as one fragment per cell.  We therefore use *coordinate* extraction
(pypdf's visitor callback gives x/y per text fragment), cluster fragments into
visual rows by their y baseline, and map x-ranges to the known table columns.

Header label/value pairs come from the lines above the goods table.

Multi-invoice PDFs are supported: a page whose header carries a new invoice
number starts a new document.
"""

from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

from common import catalog
from app.importer.base import (
    ParsedInvoice,
    SourceParseError,
    match_header_label,
    match_item_label,
    normalize_header,
    normalize_item,
)
from common.formatting import clean_text

ITEM_KEYS = ["item_no", "hs_code", "goods_name", "spec_model", "quantity",
             "unit", "unit_price", "total_price", "origin_country",
             "destination_country", "elements"]
ROW_BAND = 4          # pts; fragments within this y distance are the same row
COL_GROUP_GAP = 18    # pts; x gap large enough to signal a new column


# ---------------------------------------------------------------------------
# Coordinate extraction
# ---------------------------------------------------------------------------
def _page_fragments(page) -> list[dict]:
    frags: list[dict] = []

    def visitor(text, cm, tm, font_dict, font_size):
        if not text or not text.strip():
            return
        x = tm[4]
        y = tm[5]
        scale = (tm[0] ** 2 + tm[1] ** 2) ** 0.5 or 1.0
        frags.append({
            "text": text.replace("\r", "").replace("\n", ""),
            "x": round(x, 1), "y": round(y, 1),
            "size": round(font_size * scale, 2),
        })

    page.extract_text(visitor_text=visitor)
    return frags


def _cluster_rows(frags: list[dict]) -> list[dict]:
    """Group fragments into visual rows ordered top->bottom."""
    rows: list[dict] = []
    for frag in sorted(frags, key=lambda f: (-f["y"], f["x"])):
        target = None
        for row in rows:
            if abs(row["y"] - frag["y"]) <= ROW_BAND:
                target = row
                break
        if target is None:
            target = {"y": frag["y"], "frags": []}
            rows.append(target)
        target["frags"].append(frag)
    for row in rows:
        row["frags"].sort(key=lambda f: f["x"])
        row["text"] = "  ".join(f["text"].strip() for f in row["frags"] if f["text"].strip())
    rows.sort(key=lambda r: -r["y"])
    return rows


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------
HEADER_VALUE_RE = re.compile(r"([^:：]+)[:：]\s*([^:：]*?)(?=\s{3,}[^:：]+[:：]|$)")


def _header_from_rows(rows: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows:
        text = row["text"]
        # Several label:value pairs may share one extracted line.
        for name, value in HEADER_VALUE_RE.findall(text):
            name = clean_text(name)
            # Strip a leading English label: "境内发货人 Consignor" -> 境内发货人
            canon = match_header_label(name) or match_header_label(name.split("/")[-1])
            if not canon:
                zh = re.sub(r"[A-Za-z][A-Za-z .()]*$", "", name).strip()
                canon = match_header_label(zh)
            if canon:
                out[canon] = clean_text(value)
    return out


# ---------------------------------------------------------------------------
# Item table
# ---------------------------------------------------------------------------
def _is_item_header_row(row: dict) -> bool:
    tokens_all = row["text"].strip().split("  ")
    keys = {match_item_label(t) for t in tokens_all}
    if len([k for k in keys if k and k in ITEM_KEYS]) >= 4:
        return True
    keys = {match_item_label(f["text"]) for f in row["frags"]}
    return len([k for k in keys if k and k in ITEM_KEYS]) >= 4


def _table_columns(header_row: dict) -> tuple[list[tuple[float, str]], float, str]:
    """Return ([(x_start, key)], elements_x, style).

    style == "fragmented": each cell is its own text fragment at its own x.
    style == "rowtext":    the whole row is one fragment, cells separated by
                           runs of spaces.
    """
    label_frags = [(f["x"], match_item_label(f["text"])) for f in header_row["frags"]]
    real = [(x, k) for x, k in label_frags if k in ITEM_KEYS]
    if len(real) >= 4 and len(header_row["frags"]) >= 4:
        elements_x = next((x for x, k in label_frags if k == "elements"), None)
        cols = [(x, k) for x, k in real if k != "elements"]
        if elements_x is None and cols:
            elements_x = max(x for x, _ in cols) + 80
        return cols, elements_x, "fragmented"

    # Use the row's own tokenisation (2+ spaces) so header and data rows agree.
    tokens = header_row["text"].strip().split("  ")
    keys = [(float(i), match_item_label(t)) for i, t in enumerate(tokens)]
    real = [(i, k) for i, k in keys if k in ITEM_KEYS]
    cols = [(float(i), k) for i, k in real if k != "elements"]
    elements_x = 1e9  # everything after the numeric columns is elements
    return cols, elements_x, "rowtext"


NUMERIC_TAIL_KEYS = ["quantity", "unit", "unit_price", "total_price"]


def _detect_elements_x(rows: list[dict], header_idx: int, fallback: float) -> float:
    """Find where the right-hand (申报要素) column starts.

    Works for both layouts: look at the data rows' fragment x positions.  If they
    split into a left block and a clearly separated right block, the right one is
    the elements column.
    """
    xs = sorted({f["x"] for row in rows[header_idx + 1:] for f in row["frags"]})
    if not xs:
        return fallback
    groups: list[list[float]] = [[xs[0]]]
    for x in xs[1:]:
        if x - groups[-1][-1] >= 60:
            groups.append([x])
        else:
            groups[-1].append(x)
    if len(groups) >= 2 and groups[-1][0] - groups[0][-1] > 80:
        return groups[-1][0]
    return fallback


def _row_tokens(row: dict, elements_x: float) -> list[dict]:
    """Flatten a row into positioned tokens, splitting space-separated cells."""
    out: list[dict] = []
    for frag in row["frags"]:
        if frag["x"] >= elements_x - 2:
            out.append(dict(frag))
            continue
        text = frag["text"].strip()
        tokens = re.split(r"\s{2,}", text)
        if len(tokens) <= 1:
            out.append(dict(frag))
            continue
        # Virtual x positions keep positional mapping sane.
        approx = max(4, len(text) * 1.0)
        start = frag["x"]
        for token in tokens:
            if not token:
                continue
            out.append({"text": token.strip(), "x": start, "y": frag["y"],
                        "size": frag["size"], "virtual": True})
            start += max(8.0, approx * len(token) / max(1, len(text)))
    return out


def _row_to_item(row: dict, cols: list[tuple[float, str]], elements_x: float,
                 style: str) -> dict | None:
    tokens = _row_tokens(row, elements_x)
    # ---- rowtext style: assign by token index, reserving the numeric tail ----
    if style == "rowtext":
        left = [t for t in tokens if "virtual" in t or
                (elements_x > 1e8 or t["x"] < elements_x - 2)]
        # Never treat the 申报要素 column as a data cell.
        elem = [t for t in tokens if "virtual" not in t and
                elements_x < 1e8 and t["x"] >= elements_x - 2]
        if not left:
            return None
        left_tokens = [t["text"] for t in left]
        keys = [k for _, k in cols]
        out: dict[str, str] = {}
        if len(left_tokens) >= len(keys):
            extra = left_tokens[len(keys):]
            mid_start = keys.index("goods_name") if "goods_name" in keys else 2
            for key, tok in zip(keys[:mid_start + 1], left_tokens[:mid_start + 1]):
                out[key] = clean_text(tok)
            for key, tok in zip(keys[mid_start + 1:], left_tokens[-len(keys[mid_start + 1:]):]):
                out[key] = clean_text(tok)
            if extra:
                out["goods_name"] = clean_text(
                    out.get("goods_name", "") + " " + " ".join(extra))
        else:
            head = left_tokens[:len(left_tokens) - 4]
            tail = left_tokens[len(left_tokens) - 4:]
            for key, tok in zip(keys[:len(head)], head):
                out[key] = clean_text(tok)
            for key, tok in zip(NUMERIC_TAIL_KEYS, tail):
                out[key] = clean_text(tok)
        if elem:
            out["elements"] = clean_text("".join(t["text"] for t in elem))
        if "hs_code" not in out and "goods_name" not in out:
            return None
        return out

    # ---- fragmented style: assign by x ranges ----
    assigned: dict[str, list[dict]] = {}
    for frag in tokens:
        key = None
        for idx, (x, canon) in enumerate(cols):
            nx = cols[idx + 1][0] if idx + 1 < len(cols) else elements_x
            if x - 1 <= frag["x"] < nx - 1:
                key = canon
                break
        if key is None:
            key = "elements" if frag["x"] >= elements_x - 2 else None
        if key:
            assigned.setdefault(key, []).append(frag)
    if not assigned:
        return None
    out = {}
    for key, pieces in assigned.items():
        pieces.sort(key=lambda f: f["x"])
        text = "".join(p["text"] for p in pieces).strip() if key == "elements" else \
            " ".join(p["text"].strip() for p in pieces).strip()
        out[key] = clean_text(text)
    if "hs_code" not in out and "goods_name" not in out:
        return None
    return out


def _parse_items(rows: list[dict], style: str = "auto") -> list[dict]:
    header_idx = None
    cols: list[tuple[float, str]] = []
    elements_x = 0.0
    detected_style = style
    for idx, row in enumerate(rows):
        if _is_item_header_row(row):
            cols, elements_x, detected_style = _table_columns(row)
            header_idx = idx
            break
    if header_idx is None:
        return []
    if detected_style == "rowtext":
        elements_x = _detect_elements_x(rows, header_idx, elements_x)
    raw_items: list[dict] = []
    current: dict | None = None
    for row in rows[header_idx + 1:]:
        text = row["text"].strip()
        if re.match(r"^(合计|总计|总金额|total|subtotal|备注|声明|签章|页脚|\(?盖章|（盖章)", text, re.I):
            break
        if _is_item_header_row(row):
            continue
        parsed = _row_to_item(row, cols, elements_x, detected_style)
        if parsed is None:
            # Continuation row: only element fragments in the right column.
            if current is not None and row["frags"] and not any(
                    re.search(r"[\d.]", f["text"]) and f["x"] < elements_x - 2
                    for f in row["frags"]):
                cont = "".join(f["text"] for f in row["frags"]
                               if f["x"] >= elements_x - 2 or "virtual" not in f)
                if cont:
                    current["elements"] = clean_text(current.get("elements", "") + cont)
            continue
        has_tail = any(k in parsed for k in ("quantity", "unit_price", "total_price"))
        if has_tail or current is None:
            if current is not None:
                raw_items.append(current)
            current = parsed
        else:
            for k in ("goods_name", "spec_model", "elements"):
                if k in parsed:
                    current[k] = clean_text(current.get(k, "") + " " + parsed[k])
    if current is not None:
        raw_items.append(current)
    return raw_items


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def parse_pdf(path: str | Path) -> list[ParsedInvoice]:
    path = Path(path)
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        raise SourceParseError(f"无法打开PDF：{exc}") from exc

    invoices: list[ParsedInvoice] = []
    current: ParsedInvoice | None = None
    saw_text = False
    for page_no, page in enumerate(reader.pages, start=1):
        try:
            frags = _page_fragments(page)
        except Exception:  # noqa: BLE001
            frags = []
        if not frags:
            continue
        saw_text = True
        rows = _cluster_rows(frags)
        header_raw = _header_from_rows(rows)
        raw_items = _parse_items(rows)
        ref = clean_text(header_raw.get("external_ref", ""))

        if current is not None and ref and ref != current.external_ref:
            invoices.append(current)
            current = None
        if current is None:
            if not header_raw:
                continue
            header = normalize_header(header_raw)
            current = ParsedInvoice(
                external_ref=header.get("external_ref") or ref
                or f"PDF-{path.stem}-P{page_no}",
                source_kind="pdf", source_file=path.name,
                page_ref=f"第{page_no}页",
                header=header, items=[],
            )
        elif page_no > 1:
            current.page_ref = f"{current.page_ref};第{page_no}页"
        hs_hint = {h["code"]: h["elements"] for h in catalog.HS_CATALOG}
        for raw in raw_items:
            item_no = len(current.items) + 1
            known = hs_hint.get(raw.get("hs_code", ""), None)
            current.items.append(normalize_item(raw, item_no,
                                                current.header.get("currency", ""),
                                                known))
    if current is not None:
        invoices.append(current)

    invoices = [inv for inv in invoices if inv.items]
    if not saw_text:
        raise SourceParseError("PDF中没有可提取的文本（可能是扫描件，需要OCR或改导Excel）")
    if not invoices:
        raise SourceParseError("PDF中未解析到商品明细行，请检查版式或改导Excel")
    for idx, inv in enumerate(invoices):
        inv.sequence_no = idx
    return invoices


def extract_pdf_text(path: str | Path) -> str:
    reader = PdfReader(str(path))
    out = []
    for page in reader.pages:
        out.append("\n".join(r["text"] for r in _cluster_rows(_page_fragments(page))))
    return "\n".join(out)
