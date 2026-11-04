"""Deterministic belief patching for agreed relaxations (D045 item 4) and counter-value normalisation.

Pure functions over ``BelievedConstraint`` / ``RelaxationOption``; uses only negotiator-side data.
``believed_hardness`` is never touched here.
"""
from __future__ import annotations

import re
from typing import Optional

from gtgen.contracts import BelievedConstraint, RelaxationOption
from gtgen.matching.evidence import fmt, parse_number_unit, parse_range, token
from gtgen.matching.units import convert

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_BARE_RE = re.compile(r"^[\s\d.\-]+$")
_ONE_SIDED = {">=", ">", "<=", "<"}


def numbers(s: Optional[str]) -> list[float]:
    return [float(x) for x in _NUM_RE.findall((s or "").replace(",", ""))]


def display_value(v: Optional[str], unit: Optional[str], kind: str) -> Optional[str]:
    """Value as shown to negotiator_interpret: bare numbers get the constraint's unit appended."""
    if v is None:
        return None
    if kind == "numeric_widen" and unit and _BARE_RE.match(v):
        return f"{v.strip()} {unit}"
    return v


def _unit_of(b: BelievedConstraint) -> Optional[str]:
    if b.unit:
        return b.unit
    p = parse_number_unit(b.value)
    return p[1] if p else None


def _with_unit(b: BelievedConstraint, v: str) -> str:
    """Keep a unit that lived inside the old value string (belief.unit is None)."""
    if b.unit is None:
        u = _unit_of(b)
        if u and _BARE_RE.match(v):
            return f"{v} {u}"
    return v


def apply_option(b: BelievedConstraint, opt: RelaxationOption) -> Optional[BelievedConstraint]:
    """Belief after the agreed relaxation; ``None`` means the belief is removed (drop). Idempotent."""
    kind, to = opt.kind, opt.to_value
    if kind == "drop":
        return None
    if kind == "doc_to_pending":
        return b if b.is_doc_proof else b.model_copy(update={"is_doc_proof": True})
    if to is None:
        return b
    if kind == "numeric_widen":
        if ".." in to:
            ns = numbers(to.replace("..", " "))
            if len(ns) < 2:
                return b
            lo, hi = ns[0], ns[1]
            cur = parse_range(b.value) if (b.operator or "").lower() == "range" else None
            if cur is not None and cur[0] <= lo and cur[1] >= hi:
                return b
            return b.model_copy(update={"operator": "range", "value": f"{fmt(lo)}..{fmt(hi)}"})
        nums = numbers(to)
        if not nums:
            return b
        new = nums[0]
        op = (b.operator or "").lower()
        cur_p = parse_number_unit(b.value)
        if op in _ONE_SIDED and cur_p is not None:
            cur = cur_p[0]
            if op in (">=", ">") and cur <= new:
                return b
            if op in ("<=", "<") and cur >= new:
                return b
        return b.model_copy(update={"value": _with_unit(b, fmt(new))})
    if kind == "categorical_extend":
        op = "not_in" if (b.operator or "").lower() == "not_in" else "in"
        cur = {token(x) for x in (b.value or "").split(",") if x.strip()} if op == "in" else set()
        want = {token(x) for x in to.split(",") if x.strip()}
        if op == "in" and b.operator == "in" and want <= cur:
            return b
        return b.model_copy(update={"operator": op, "value": to})
    return b


def normalise_counter(opt: RelaxationOption, value: str, b: BelievedConstraint) -> Optional[str]:
    """Buyer counter text -> the ``to_value`` representation the matching module uses; None if unusable."""
    if opt.kind == "categorical_extend":
        if (b.operator or "").lower() == "not_in":
            return None
        have = [token(x) for x in (opt.from_value or "").split(",") if x.strip()]
        add = token(value)
        if not add:
            return None
        return ", ".join(sorted(set(have) | {add}))
    if opt.kind != "numeric_widen":
        return None
    nums = numbers(value)
    if not nums:
        return None
    unit = _unit_of(b)
    p = parse_number_unit(value)
    if ".." in (opt.to_value or ""):
        if len(nums) >= 2:
            return f"{fmt(min(nums[:2]))}..{fmt(max(nums[:2]))}"
        t = numbers(opt.from_value)
        if not t:
            return None
        d = abs(nums[0] - t[0])
        return f"{fmt(t[0] - d)}..{fmt(t[0] + d)}"
    n = nums[0]
    if p is not None and p[1] and unit and p[1] != unit:
        c = convert(n, p[1], unit)
        if c is None:
            return None
        n = c
    return fmt(n)


def option_cost(opt: RelaxationOption, new_to: str) -> float:
    """Normalised cost of ``opt`` re-targeted at ``new_to`` (numeric only; else the original cost)."""
    f, t = numbers(opt.from_value), numbers(new_to.replace("..", " "))
    if opt.kind != "numeric_widen" or not f or not t or f[0] == 0:
        return opt.normalised_cost
    if ".." in new_to and len(t) >= 2:
        return round(min(1.0, (t[1] - t[0]) / 2.0 / abs(f[0])), 12)
    return round(min(1.0, abs(t[0] - f[0]) / abs(f[0])), 12)


def value_equals(belief_value: Optional[str], counter_value: Optional[str]) -> bool:
    a, b = numbers((belief_value or "").replace("..", " ")), numbers((counter_value or "").replace("..", " "))
    return bool(a) and len(a) == len(b) and all(abs(x - y) <= 1e-6 * max(1.0, abs(x), abs(y)) for x, y in zip(a, b))
