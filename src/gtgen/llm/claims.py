"""Document-claim helpers (D051): listing text, quote verification, cost-control prefilter."""
from __future__ import annotations

import re
from typing import Any

from gtgen.contracts import CandidateView

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9]+")
_STD_ID = re.compile(r"\b(?:IS|ISO|IEC|BIS|EN|ASTM|DIN|BS|UL|CE)\b[\s/:.-]*\d[\w.:/-]*", re.I)
_STOP = frozenset({"the", "and", "for", "with", "per", "any", "all", "not", "from", "shall", "must", "are", "has",
                   "have", "this", "that", "its", "type", "new", "etc"})
MIN_QUOTE_CHARS = 4


def _squash(s: str) -> str:
    return _WS.sub(" ", s).strip().casefold()


def listing_text(candidate: CandidateView, max_chars: int) -> str:
    """Specification 'key: value' lines, then description, then brochure_text, truncated to max_chars."""
    parts: list[str] = []
    if candidate.specifications:
        parts += [f"{k}: {v}" for k, v in candidate.specifications.items()]
    elif candidate.specifications_raw:
        parts.append(candidate.specifications_raw)
    if candidate.description:
        parts.append(candidate.description)
    if candidate.brochure_text:
        parts.append(candidate.brochure_text)
    return "\n".join(parts)[:max(0, int(max_chars))]


def verify_quote(quote: Any, listing: str) -> bool:
    """Case-insensitive, whitespace-collapsed substring test. Quotes under 4 characters never verify."""
    if not isinstance(quote, str):
        return False
    q = _squash(quote)
    if len(q) < MIN_QUOTE_CHARS:
        return False
    return q in _squash(listing or "")


def maybe_claims(listing: str, doc_name: str, settings: dict) -> bool:
    """Cheap prefilter (generous on purpose). Always True when doc_claims.prefilter is false."""
    cfg = settings.get("doc_claims", {})
    if not cfg.get("prefilter", True):
        return True
    low = _squash(listing or "")
    if not low:
        return False
    name = _squash(doc_name or "")
    for tok in _WORD.findall(name):
        if len(tok) >= 3 and tok not in _STOP and tok in low:
            return True
    for m in _STD_ID.finditer(doc_name or ""):
        ident = _squash(m.group(0))
        if ident in low or re.sub(r"[\s/:.-]+", "", ident) in re.sub(r"[\s/:.-]+", "", low):
            return True
    return any(_squash(t) in low for t in cfg.get("generic_cert_terms", []) if t)
