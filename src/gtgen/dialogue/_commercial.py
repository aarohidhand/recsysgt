"""Negotiator-side post-processing of extracted beliefs (r5 audit): order quantity is not a requirement, and the
commercial terms (budget, minimum order quantity) default to SOFT unless the buyer is explicit.

Uses only the transcript text and the negotiator's own beliefs (barrier-safe). ``believed_hardness`` is changed only
here, at extraction time, never after a refusal (D045)."""
from __future__ import annotations

import re
from typing import Optional, Sequence

from gtgen.contracts import BelievedConstraint, Hardness, Speaker, TranscriptView

from ._util import name_tokens, norm_attr

_QTY_WORDS = frozenset({"quantity", "qty", "units", "unit", "pieces", "pcs", "nos", "number"})
_LIMIT_WORDS = frozenset({"minimum", "min", "moq", "maximum", "max", "vs", "per", "lot", "batch", "delivery", "lead", "time",
                          "weight", "size", "rated", "capacity", "pack", "carton"})
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_EXPLICIT_RE = re.compile(r"\b(?:non[- ]?negotiable|strict(?:ly)?|absolutely|firm(?:ly)?|hard (?:limit|cap|ceiling))\b", re.I)
_SENT_RE = re.compile(r"[!?;\n]+|\.(?!\d)|(?<!\d)\.")


def _num(s: Optional[str]) -> Optional[float]:
    m = _NUM_RE.search((s or "").replace(" ", ""))
    try:
        return float(m.group(0).replace(",", "")) if m else None
    except ValueError:
        return None


def is_order_quantity_belief(b: BelievedConstraint, order_qty: Optional[float]) -> bool:
    """The belief states how many items the buyer orders (a brief field), not a requirement. MOQ-style limits
    (minimum / maximum / qty_vs_moq ...) are kept."""
    toks = name_tokens(b.attribute) | {t for t in re.split(r"[^a-z]+", (b.attribute or "").lower()) if t}
    if not toks & _QTY_WORDS or toks & _LIMIT_WORDS:
        return False
    if (b.operator or "=").strip() not in ("=", "==", ""):
        return False
    n = _num(b.value)
    if order_qty is not None:
        return n is not None and abs(n - order_qty) < 1e-9
    return toks <= (_QTY_WORDS | {"order", "ordered", "required", "total", "of", "item", "items"})   # bare 'quantity'


def commercial_kind(b: BelievedConstraint) -> Optional[str]:
    """'budget' | 'moq' for the two synthetic commercial terms, else None.

    Delegates to ``gtgen.matching.compliance._commercial_kind`` (same heuristic, generalised to truth or belief
    constraints for the D075/D081 verified-feasible gate); kept here under its original name/signature since
    callers in this module import it from here."""
    from gtgen.matching.compliance import _commercial_kind
    return _commercial_kind(b)


def _kind_mentioned(kind: str, sentence: str) -> bool:
    s = sentence.lower()
    return "budget" in s if kind == "budget" else bool(re.search(r"\bmoq\b|minimum order|order quantity|min\.? order", s))


def default_soft_commercial(beliefs: Sequence[BelievedConstraint], transcript: TranscriptView
                            ) -> tuple[list[BelievedConstraint], int]:
    """Budget and MOQ beliefs are SOFT unless a buyer sentence about that term has an explicit firm cue
    ('non-negotiable', 'strict', 'absolutely', ...). Returns (beliefs, number changed)."""
    sents = [x.strip() for t in transcript.turns if t.speaker == Speaker.BUYER for x in _SENT_RE.split(t.utterance) if x.strip()]
    out: list[BelievedConstraint] = []
    n = 0
    for b in beliefs:
        kind = commercial_kind(b)
        if kind is None:
            out.append(b)
            continue
        hits = [s for s in sents if _kind_mentioned(kind, s)]
        want = Hardness.HARD if (hits and _EXPLICIT_RE.search(hits[-1])) else Hardness.SOFT
        if b.believed_hardness != want:
            b = b.model_copy(update={"believed_hardness": want})
            n += 1
        out.append(b)
    return out, n
