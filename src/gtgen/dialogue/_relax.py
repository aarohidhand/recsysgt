"""Pure helpers shared by the buyer and the negotiator for the D053 level model.

The two sides never share state; they only share this arithmetic, which turns the VALUES that appear in a proposal
(the negotiator's ``RelaxationOption.from_value`` / ``to_value``) into a relative change and the relative change into a
level index of a rule (levels are the same grid on both sides: ``relaxation.levels`` x ``default_max_deviation``).
"""
from __future__ import annotations

import re
from typing import Optional, Sequence

from gtgen.matching.evidence import fmt

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
EPS = 1e-9


def nums(s: Optional[str]) -> list[float]:
    return [float(x) for x in _NUM_RE.findall((s or "").replace(",", ""))]


def change_fraction(from_value: Optional[str], to_value: Optional[str]) -> Optional[float]:
    """Relative change of a proposal: |to - from| / |from| for scalars; for a band target (``lo..hi``) the larger
    half-width over |from|; for a range -> range the change of the lower end. ``None`` if not computable."""
    f = nums(from_value)
    t = nums((to_value or "").replace("..", " "))
    if not f or not t:
        return None
    if len(f) == 1 and len(t) == 1:
        return abs(t[0] - f[0]) / abs(f[0]) if f[0] else None
    if len(f) == 1:
        return max(abs(f[0] - t[0]), abs(t[1] - f[0])) / abs(f[0]) if f[0] else None
    if len(t) >= 2:
        return abs(f[0] - t[0]) / abs(f[0]) if f[0] else None
    return None


def level_ceil(levels: Sequence, fraction: float) -> Optional[int]:
    """Smallest level index whose relative change is >= ``fraction`` (the level that covers a proposed value)."""
    for i, lv in enumerate(levels):
        if float(lv) >= fraction - EPS:
            return i
    return None


def level_floor(levels: Sequence, fraction: float) -> int:
    """Largest level index whose relative change is <= ``fraction`` (the level a counter value stands for)."""
    best = 0
    for i, lv in enumerate(levels):
        if float(lv) <= fraction + EPS:
            best = i
    return best


def scale_value(from_value: Optional[str], to_value: Optional[str], ratio: float) -> Optional[str]:
    """The value that lies ``ratio`` of the way from ``from_value`` to ``to_value`` (same shape as ``to_value``)."""
    f = nums(from_value)
    t = nums((to_value or "").replace("..", " "))
    if not f or not t:
        return None
    if len(t) == 1 and len(f) == 1:
        return fmt(round(f[0] + (t[0] - f[0]) * ratio, 6))
    if len(t) >= 2 and len(f) == 1:
        lo, hi = f[0] + (t[0] - f[0]) * ratio, f[0] + (t[1] - f[0]) * ratio
        return f"{fmt(round(lo, 6))}..{fmt(round(hi, 6))}"
    if len(t) >= 2 and len(f) >= 2:
        lo, hi = f[0] + (t[0] - f[0]) * ratio, f[1] + (t[1] - f[1]) * ratio
        return f"{fmt(round(lo, 6))}..{fmt(round(hi, 6))}"
    return None


def bound_snapshot(bounds) -> dict:
    return {k: dict(v) for k, v in bounds.items()}


# ---------------------------------------------------------------- D076: supplier-state widening (categorical set rule)
_ANY_STATE = ("any state", "anywhere", "all states", "any location")


def _join_names(names: Sequence[str]) -> str:
    names = [str(n) for n in names]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def state_phrase(added: Sequence[str], any_state: bool) -> str:
    """Plain-language statement of a state widening (no internal names, no digits)."""
    if any_state or not added:
        return "suppliers from anywhere"
    return f"also consider suppliers from {_join_names(added)}"


def state_level_from_text(rule, text: Optional[str]) -> Optional[int]:
    """Smallest level of the state rule ``rule`` whose set covers the states named in ``text`` (a list of states, 'any
    state', or free wording that names states such as 'also consider suppliers from Rajasthan'); None if no state of the
    rule is named."""
    from gtgen.matching import state_level_for
    from gtgen.matching.states import norm_state
    if rule is None or getattr(rule, "mode", None) != "set" or not text:
        return None
    k = state_level_for(rule, text)
    if k is not None:
        return k
    low = " ".join(str(text).lower().replace("&", " and ").split())
    if any(a in low for a in _ANY_STATE):
        return rule.top
    found = {norm_state(st) for lv in rule.level_sets for st in lv if norm_state(st) in low}
    if not found:
        return None
    for k, st in enumerate(rule.level_sets):
        if found <= {norm_state(x) for x in st}:
            return k
    return None
