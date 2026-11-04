"""Shared tiny report primitives for the deterministic audit modules (persona_diversity, realism)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
_ORDER = {SKIP: 0, PASS: 1, WARN: 2, FAIL: 3}


@dataclass
class Check:
    name: str
    verdict: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def worst(checks: list[Check]) -> str:
    """Overall verdict = the worst individual verdict (FAIL > WARN > PASS > SKIP)."""
    return max((c.verdict for c in checks), key=_ORDER.__getitem__, default=SKIP)


def norm_text(s: str) -> str:
    """Lower-case, punctuation to spaces, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def text_similarity(a: str, b: str) -> float:
    """max(token-set Jaccard, character SequenceMatcher ratio) on normalised text, in [0, 1]."""
    na, nb = norm_text(a), norm_text(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    jac = len(ta & tb) / len(ta | tb)
    return max(jac, SequenceMatcher(None, na, nb).ratio())
