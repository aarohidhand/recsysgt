"""D065/D070: natural number formatting for spoken text (applied in code BEFORE values reach a prompt).

Floats such as ``500.0``, ``215335.9375`` or ``43.38`` (units) must never be shown to the model, or it repeats them.
"""
from __future__ import annotations

import re
from typing import Any, Optional


def indian_group(n: int) -> str:
    """Lakh/crore grouping: 270562 -> '2,70,562'."""
    s = str(abs(int(n)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-" if n < 0 else "") + s


def format_inr(value: Any) -> Optional[str]:
    """'INR 2,70,562' for any money amount (rounded to whole rupees); None stays None."""
    if value is None or value == "":
        return None
    return f"INR {indian_group(int(round(float(value))))}"


def format_quantity(value: Any) -> Optional[str]:
    """Counts are whole numbers: 500.0 -> '500', 43.38 -> '43'; small fractional amounts keep up to 2 decimals."""
    if value is None or value == "":
        return None
    v = float(value)
    if abs(v - round(v)) < 1e-9 or abs(v) >= 10:
        return str(int(round(v)))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def format_plain(value: Any) -> str:
    """A plain number: no trailing '.0', at most 2 decimals; >= 100 with a long fraction rounds to whole."""
    v = float(value)
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    if abs(v) >= 100:
        return str(int(round(v)))
    return f"{v:.2f}".rstrip("0").rstrip(".")


_TRAILING_ZERO = re.compile(r"(?<![\w.])(\d[\d,]*)\.0+(?![\d.]*\d)")
_LONG_FRACTION = re.compile(r"(?<![\w.])(\d[\d,]*\.\d{3,})(?!\d)")


def tidy_numbers(text: Optional[str]) -> Optional[str]:
    """Rewrite float artefacts inside a sentence: '500.0 units' -> '500 units', '215335.9375 INR' -> '215336 INR',
    '0.123456' -> '0.12'. Version-like tokens (2.3.4, ISO 9001:2015) are untouched."""
    if not text:
        return text

    def long_frac(m: re.Match) -> str:
        try:
            return format_plain(float(m.group(1).replace(",", "")))
        except ValueError:
            return m.group(0)

    out = _LONG_FRACTION.sub(long_frac, text)
    return _TRAILING_ZERO.sub(lambda m: m.group(1), out)


def tidy_value(value: Any) -> Any:
    """``tidy_numbers`` for a string value; other types pass unchanged."""
    return tidy_numbers(value) if isinstance(value, str) else value
