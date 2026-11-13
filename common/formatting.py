"""Normalisation helpers shared by parsers, validators and the comparator."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CURRENCY_SYMBOLS = ("US$", "$", "¥", "￥", "€", "£")
_WS_RE = re.compile(r"\s+")
_FULLWIDTH = str.maketrans("０１２３４５６７８９．，：；（）／％", "0123456789.,:;()/%")


def clean_text(value) -> str:
    if value is None:
        return ""
    text = str(value).translate(_FULLWIDTH).replace(" ", " ")
    return _WS_RE.sub(" ", text).strip()


def to_decimal(value) -> Decimal | None:
    """Parse money/quantity values that may carry symbols, commas or spaces."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = clean_text(value)
    if not text:
        return None
    for sym in CURRENCY_SYMBOLS:
        text = text.replace(sym, "")
    text = text.replace(",", "").replace(" ", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def money(value) -> Decimal | None:
    d = to_decimal(value)
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if d is not None else None


def quantity(value) -> Decimal | None:
    d = to_decimal(value)
    return d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if d is not None else None


def fmt_decimal(value, places: int = 2) -> str:
    d = to_decimal(value)
    if d is None:
        return clean_text(value)
    q = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{q:f}"


def parse_elements(raw, known_keys: list[str] | None = None) -> dict[str, str]:
    """Parse ``品牌类型=无品牌; 型号=T14`` into a dict.

    Also tolerates newline- or comma-separated pairs and ``：`` separators.
    ``known_keys`` lets us recover pairs whose separator was lost to PDF line
    wrapping (``…优惠关税品牌=LENOVO``), by splitting in front of a known key.
    """
    if isinstance(raw, dict):
        return {clean_text(k): clean_text(v) for k, v in raw.items()}
    text = clean_text(raw)
    if not text:
        return {}
    if known_keys:
        # Insert a separator in front of a known key when none precedes it.
        pattern = "|".join(re.escape(k) for k in sorted(known_keys, key=len, reverse=True))
        text = re.sub(r"(?<=[^;；=：:])(?=(?:%s)=)" % pattern, "；", text)
    parts = re.split(r"[;；\n]+", text)
    out = {}
    for part in parts:
        part = part.strip().strip("。")
        if not part:
            continue
        m = re.match(r"^([^=:：]+)[=:：]\s*(.*)$", part)
        if m:
            key = clean_text(m.group(1))
            val = clean_text(m.group(2))
        else:
            key, val = "", clean_text(part)
        if key:
            out[key] = val
    return out


def parse_mode(raw) -> str:
    """Extract a trade-mode code from ``进料对口(0615)`` / ``0615`` / name."""
    from common.catalog import TRADE_MODES, TRADE_MODE_BY_CODE

    text = clean_text(raw)
    if not text:
        return ""
    m = re.search(r"[（(]\s*(\d{4})\s*[)）]", text)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{4})\b", text)
    if m and m.group(1) in TRADE_MODE_BY_CODE:
        return m.group(1)
    for mode in TRADE_MODES:
        if mode["name"] in text:
            return mode["code"]
    return text if text in TRADE_MODE_BY_CODE else ""


def parse_currency(raw) -> str:
    from common.catalog import CURRENCIES

    text = clean_text(raw).upper()
    if not text:
        return ""
    m = re.search(r"\b([A-Z]{3})\b", text)
    if m and m.group(1) in CURRENCIES:
        return m.group(1)
    for c in CURRENCIES:
        if c in text:
            return c
    return text


def parse_date(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    text = clean_text(raw)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def values_equal(field: str, expected, actual) -> bool:
    """Field-aware comparison used by the read-back verifier."""
    if field in {"quantity", "unit_price", "total_price", "freight", "insurance", "bonded_ratio"}:
        a, b = to_decimal(expected), to_decimal(actual)
        if a is None or b is None:
            return clean_text(expected) == clean_text(actual)
        places = Decimal("0.0001") if field == "quantity" else Decimal("0.01")
        return abs(a - b) <= places
    if field == "trade_mode":
        return parse_mode(expected) == parse_mode(actual)
    if field == "currency":
        return parse_currency(expected) == parse_currency(actual)
    return clean_text(expected).replace(" ", "") == clean_text(actual).replace(" ", "")


def canonical_for_hash(value):
    """Stable JSON-able representation for fingerprints."""
    if isinstance(value, dict):
        return {str(k): canonical_for_hash(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [canonical_for_hash(v) for v in value]
    if isinstance(value, Decimal):
        return f"{value.normalize():f}"
    if value is None:
        return ""
    return clean_text(value) if isinstance(value, str) else value
