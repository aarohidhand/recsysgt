"""Constraint views, candidate evidence extraction and the pure comparison core.

Everything here is deterministic and side-effect free. ``compliance`` and ``relaxation`` both build
on ``evaluate`` so a relaxed constraint is re-checked with exactly the same logic as the original.

D019: ``*_raw`` text (allowed_values_raw / specifications_raw) is never parsed -> no usable evidence.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Optional, Protocol, Sequence, Union, runtime_checkable

from gtgen.contracts import (
    AttributeAlignment,
    BelievedConstraint,
    BuyerConstraint,
    CandidateConstraint,
    CandidateView,
    Compliance,
    Hardness,
    NormalisedValue,
)
from gtgen.matching.units import convert, norm_unit

DEFAULT_EQ_REL_TOL = 1.0e-6  # negotiation.bundle_search.numeric_eq_rel_tolerance


# ------------------------------------------------------------------ common protocol
@runtime_checkable
class ConstraintLike(Protocol):
    """Anything ``as_view`` can adapt: BuyerConstraint (truth) or BelievedConstraint (belief)."""
    attribute: str
    operator: Optional[str]
    unit: Optional[str]
    is_doc_proof: bool


ConstraintT = Union[BuyerConstraint, BelievedConstraint]

_OP_ALIASES = {"==": "=", "eq": "=", "equals": "=", "≥": ">=", "ge": ">=", "≤": "<=", "le": "<=",
               "gt": ">", "lt": "<", "<>": "!=", "ne": "!=", "not in": "not_in", "notin": "not_in",
               "between": "range", "one_of": "in"}
OPS = {"=", "!=", ">=", "<=", ">", "<", "in", "not_in", "range", "contains"}
NEGATED = {"no", "none", "false", "nil", "n/a", "na", "not available", "not applicable", "absent", "-"}


def norm_op(op: Optional[str]) -> Optional[str]:
    if op is None:
        return None
    o = op.strip().lower()
    return _OP_ALIASES.get(o, o)


@dataclass(frozen=True)
class ConstraintView:
    """Uniform read-only view of a (believed) constraint. ``rng`` is set by relaxation of numeric ``=``."""
    ref: str
    attribute: str
    operator: Optional[str]
    value: Optional[str]
    unit: Optional[str]
    allowed: Optional[tuple[str, ...]]
    allowed_unparsed: bool
    allows_higher: bool
    hardness: Hardness
    is_doc_proof: bool
    doc_name: Optional[str]
    category: Optional[str]
    rng: Optional[tuple[float, float]] = None


def ref_of(c: ConstraintT) -> str:
    return c.constraint_id if isinstance(c, BuyerConstraint) else c.belief_id


def hardness_of(c: ConstraintT) -> Hardness:
    return c.hardness if isinstance(c, BuyerConstraint) else c.believed_hardness


def _split_list(s: str) -> tuple[str, ...]:
    s = s.strip()
    if s.startswith("["):
        try:
            v = json.loads(s)
            if isinstance(v, list):
                return tuple(str(x) for x in v)
        except ValueError:
            pass
    parts = re.split(r"[,;|]", s)
    return tuple(p.strip() for p in parts if p.strip())


def as_view(c: ConstraintT) -> ConstraintView:
    if isinstance(c, BuyerConstraint):
        allowed = tuple(c.allowed_values) if c.allowed_values is not None else None
        unparsed = allowed is None and bool(c.allowed_values_raw)
        return ConstraintView(
            ref=c.constraint_id, attribute=c.attribute, operator=norm_op(c.operator), value=c.buyer_value,
            unit=c.unit, allowed=allowed, allowed_unparsed=unparsed, allows_higher=bool(c.allows_higher),
            hardness=c.hardness, is_doc_proof=c.is_doc_proof, doc_name=c.doc_name,
            category=c.constraint_category)
    return ConstraintView(
        ref=c.belief_id, attribute=c.attribute, operator=norm_op(c.operator), value=c.value, unit=c.unit,
        allowed=None, allowed_unparsed=False, allows_higher=False, hardness=c.believed_hardness,
        is_doc_proof=c.is_doc_proof, doc_name=None, category=None)


# ------------------------------------------------------------------ parsing helpers
_NUM = r"[-+]?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?|[-+]?\.\d+"
_NUM_UNIT = re.compile(rf"^\s*({_NUM})\s*([A-Za-z%°/²³\.]*)\s*$")


def parse_number_unit(s: Optional[str]) -> Optional[tuple[float, Optional[str]]]:
    if s is None:
        return None
    m = _NUM_UNIT.match(str(s))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "")), (m.group(2) or None)
    except ValueError:
        return None


def parse_range(s: Optional[str]) -> Optional[tuple[float, float]]:
    if s is None:
        return None
    m = re.search(rf"({_NUM})\s*(?:\.\.|to|–|-|,)\s*({_NUM})", str(s))
    if not m:
        return None
    a, b = float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
    return (min(a, b), max(a, b))


_WORD = re.compile(r"[a-z]+")


def _stem_word(w: str) -> str:
    """Conservative stem for categorical comparison only ("flanged"/"flange"/"flanges" -> "flang").
    Words of <= 4 letters are untouched; "-ss" words (stainless) keep their s."""
    if len(w) <= 4:
        return w
    if w.endswith("ing") and len(w) > 5:
        w = w[:-3]
    elif w.endswith("ed"):
        w = w[:-2]
    elif w.endswith("es") and not w.endswith("ses"):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w[:-1] if w.endswith("e") and len(w) > 3 else w


def token(s: object) -> str:
    """Canonical comparison token: numbers by float value, text lower-cased, stripped and lightly stemmed
    (plural / -ed / -ing / trailing -e; D070 audit defect 9: "Flanged" == "Flange").  Comparison only, never display."""
    t = str(s).strip().lower()
    p = parse_number_unit(t)
    if p is not None and p[1] is None:
        return repr(float(p[0]))
    t = re.sub(r"\s+", " ", t)
    return _WORD.sub(lambda m: _stem_word(m.group(0)), t)


def close(a: float, b: float, rel_tol: float = DEFAULT_EQ_REL_TOL) -> bool:
    return abs(a - b) <= rel_tol * max(1.0, abs(a), abs(b))


def fmt(x: float) -> str:
    return f"{x:g}"


# ------------------------------------------------------------------ evidence
@dataclass(frozen=True)
class Evidence:
    usable: bool = False                  # some parsed value present
    lo: Optional[float] = None            # numeric interval in the buyer's unit
    hi: Optional[float] = None
    unit_unknown: bool = False            # numeric evidence whose unit cannot be converted
    values: tuple[str, ...] = ()          # canonical tokens (see ``token``)
    display: tuple[str, ...] = ()         # original strings, same order as values
    text: str = ""                        # lower-cased blob for ``contains``
    claim: bool = False                   # candidate asserts it has the attribute / document
    confidence: Optional[float] = None
    verified: Optional[bool] = None
    summary: Optional[str] = None


def _candidate_texts(cand: CandidateView) -> str:
    parts = [cand.product_name or "", cand.description or "", cand.brochure_text or ""]
    parts += [c.raw_text or "" for c in cand.constraints]
    return " ".join(parts).lower()


def _match_rows(cand: CandidateView, cand_attr: str) -> list[CandidateConstraint]:
    a = cand_attr.strip().lower()
    return [c for c in cand.constraints
            if c.attribute.strip().lower() == a or (c.spec_name or "").strip().lower() == a]


def extract_evidence(view: ConstraintView, cand: CandidateView, alignment: Optional[AttributeAlignment],
                     normalised: Optional[NormalisedValue] = None) -> Evidence:
    text_blob = _candidate_texts(cand)
    doc_claim = bool(view.doc_name) and view.doc_name.strip().lower() in text_blob
    aligned = alignment is not None and bool(alignment.candidate_attribute)
    if not aligned and normalised is None:
        return Evidence(text=text_blob, claim=doc_claim)

    # unaligned + normalised: the normalised value (keyed by buyer attribute) is the only evidence
    ca = alignment.candidate_attribute if aligned else view.attribute
    rows = _match_rows(cand, ca) if aligned else []
    raw_vals: list[str] = []
    units: list[Optional[str]] = []
    interval: Optional[tuple[float, float, Optional[str]]] = None
    conf = verified = None
    row_exists = bool(rows)
    for r in rows:
        if conf is None:
            conf = r.confidence
        if verified is None:
            verified = r.raw_text_verified
        if r.value is not None:
            raw_vals.append(str(r.value))
            units.append(r.unit)
        for av in (r.allowed_values or []):
            raw_vals.append(str(av))
            units.append(r.unit)
        if interval is None and (r.min_value is not None or r.max_value is not None):
            lo = r.min_value if r.min_value is not None else r.max_value
            hi = r.max_value if r.max_value is not None else r.min_value
            interval = (min(lo, hi), max(lo, hi), r.unit)  # type: ignore[type-var]
    if aligned and not rows and cand.specifications:  # parsed dict only; specifications_raw is never parsed
        for k, v in cand.specifications.items():
            if str(k).strip().lower() == ca.strip().lower() and v is not None and not isinstance(v, (dict, list)):
                raw_vals.append(str(v))
                units.append(None)
                row_exists = True
    if normalised is not None:
        raw_vals = [normalised.value]
        units = [normalised.unit]
        interval = None
        row_exists = True
        conf = normalised.confidence if conf is None else conf

    negated = bool(raw_vals) and all(v.strip().lower() in NEGATED for v in raw_vals)
    claim = (row_exists and not negated) or doc_claim

    # numeric interval in the buyer's unit
    tunit = view.unit
    p = parse_number_unit(view.value)
    if tunit is None and p is not None:
        tunit = p[1]
    lo = hi = None
    unit_unknown = False
    if interval is None:
        for v, u in zip(raw_vals, units):
            pn = parse_number_unit(v)
            if pn is not None:
                interval = (pn[0], pn[0], u or pn[1])
                break
    if interval is not None:
        a = convert(interval[0], interval[2], tunit)
        b = convert(interval[1], interval[2], tunit)
        if a is None or b is None:
            unit_unknown = True
        else:
            lo, hi = min(a, b), max(a, b)
    values = tuple(token(v) for v in raw_vals)
    usable = bool(values) or lo is not None or unit_unknown
    summ = None
    if raw_vals:
        summ = f"{ca}={raw_vals[0]}" + (f" {units[0]}" if units[0] and parse_number_unit(raw_vals[0]) and
                                           parse_number_unit(raw_vals[0])[1] is None else "")
    elif interval is not None:
        summ = f"{ca}={interval[0]:g}..{interval[1]:g}" + (f" {interval[2]}" if interval[2] else "")
    return Evidence(usable=usable, lo=lo, hi=hi, unit_unknown=unit_unknown, values=values,
                    display=tuple(raw_vals), text=text_blob + " " + " ".join(v.lower() for v in raw_vals),
                    claim=claim, confidence=conf, verified=verified, summary=summ)


# ------------------------------------------------------------------ comparison core
NUMERIC_OPS = {">=", "<=", ">", "<", "range"}


def effective_op(view: ConstraintView) -> Optional[str]:
    op = view.operator
    if op is None:
        if view.allowed is not None or view.allowed_unparsed:
            return "in"
        return "=" if view.value is not None else None
    if op == "=" and view.allows_higher and parse_number_unit(view.value) is not None:
        return ">="
    return op


def target_number(view: ConstraintView) -> Optional[float]:
    p = parse_number_unit(view.value)
    return p[0] if p else None


def buyer_set(view: ConstraintView) -> Optional[tuple[str, ...]]:
    """Canonical token set the buyer allows/excludes; None if not available (incl. unparsed raw)."""
    if view.allowed is not None:
        return tuple(token(a) for a in view.allowed)
    if view.allowed_unparsed:
        return None
    if view.value is None:
        return None
    if effective_op(view) in ("in", "not_in"):
        return tuple(token(a) for a in _split_list(view.value))
    return (token(view.value),)


def _range_of(view: ConstraintView) -> Optional[tuple[float, float]]:
    if view.rng is not None:
        return view.rng
    r = parse_range(view.value)
    if r is None and view.allowed is not None and len(view.allowed) == 2:
        try:
            a, b = float(view.allowed[0]), float(view.allowed[1])
            r = (min(a, b), max(a, b))
        except ValueError:
            r = None
    return r


# Defect 6b: evidence values that only say "something of this kind exists / is bespoke" (they are not a specific value
# that can contradict a specific requirement).  Compared lower-cased and stripped.  A settings key is not used (yet).
GENERIC_EVIDENCE_VALUES = frozenset({
    "yes", "true", "available", "present", "applicable", "provided", "included", "customized", "customised",
    "custom", "customizable", "customisable", "standard", "regular", "normal", "default", "as per requirement",
    "as per requirements", "as per buyer requirement", "as per buyer's requirement", "as per buyer's requirements"})
_BOOLISH = {"yes", "true", "no", "false"}


def _is_generic_text(s: str) -> bool:
    t = re.sub(r"\s+", " ", str(s).strip().lower())
    return t in GENERIC_EVIDENCE_VALUES or t.startswith("as per ")


def type_mismatch(view: ConstraintView, ev: Evidence) -> bool:
    """Defect 3: a non-numeric (boolean / textual) requirement against purely numeric evidence, or a numeric-operator
    requirement without a number.  Such pairs cannot be compared: the caller reports UNKNOWN ('type_mismatch')."""
    if not ev.usable or ev.unit_unknown:
        return False
    op = effective_op(view)
    if op is None or op not in OPS or op == "contains":
        return False
    if op == "range" or view.rng is not None:
        return False
    num_ev = bool(ev.display) and all(parse_number_unit(d) is not None for d in ev.display)
    if not num_ev:
        return False
    if op in NUMERIC_OPS:
        return view.value is not None and target_number(view) is None and parse_range(view.value) is None
    if view.allowed is not None:
        bs: Sequence[str] = view.allowed
    elif view.value is None:
        return False
    else:
        bs = _split_list(view.value) if op in ("in", "not_in") else (view.value,)
    if not bs:
        return False
    return not any(parse_number_unit(b) is not None for b in bs)


def less_specific(view: ConstraintView, ev: Evidence) -> bool:
    """r5 audit: categorical evidence whose (stemmed) words are a PROPER SUBSET of a required value's words
    ("Cast Iron" vs "Grey Cast Iron, Gr-FG") is less specific, not contradictory -> UNKNOWN ('less_specific').
    "Ductile Iron" vs "Grey Cast Iron" is not a subset (conflicting qualifier) and stays FAIL."""
    if effective_op(view) not in ("in", "=") or view.rng is not None:
        return False
    bs = buyer_set(view)
    if not bs or not ev.display or set(ev.values) & set(bs):
        return False
    req = [str(a) for a in (view.allowed if view.allowed is not None else _split_list(view.value or ""))]
    rw = [set(_WORD.findall(token(r))) for r in req if not parse_number_unit(r)]
    for d in ev.display:
        d = str(d)
        if parse_number_unit(d) is not None or _is_generic_text(d) or d.strip().lower() in _BOOLISH:
            return False
        dw = set(_WORD.findall(token(d)))
        if not dw or not any(dw < r for r in rw):
            return False
    return True


def evaluate(view: ConstraintView, ev: Evidence, rel_tol: float = DEFAULT_EQ_REL_TOL) -> Compliance:
    """PASS / FAIL / UNKNOWN for a non-doc-proof comparison (doc-proof is handled in compliance)."""
    P, F, U = Compliance.PASS, Compliance.FAIL, Compliance.UNKNOWN
    op = effective_op(view)
    if op is None or op not in OPS or not ev.usable:
        return U
    if type_mismatch(view, ev):
        return U
    if op == "range" or view.rng is not None:
        rg = _range_of(view)
        if rg is None or ev.unit_unknown or ev.lo is None:
            return U
        lo, hi = rg
        tl, th = rel_tol * max(1.0, abs(lo)), rel_tol * max(1.0, abs(hi))
        if ev.lo >= lo - tl and ev.hi <= hi + th:
            return P
        if ev.hi < lo - tl or ev.lo > hi + th:
            return F
        return U
    t = target_number(view)
    eq_numeric = op in {"=", "!="} and t is not None and (ev.lo is not None or ev.unit_unknown)
    if op in NUMERIC_OPS or eq_numeric:
        if t is None or ev.unit_unknown or ev.lo is None:
            return U
        lo, hi = ev.lo, ev.hi
        point = close(lo, hi, rel_tol)
        ge = lambda a, b: a >= b or close(a, b, rel_tol)  # noqa: E731
        le = lambda a, b: a <= b or close(a, b, rel_tol)  # noqa: E731
        gt = lambda a, b: a > b and not close(a, b, rel_tol)  # noqa: E731
        lt = lambda a, b: a < b and not close(a, b, rel_tol)  # noqa: E731
        if op == ">=":
            return P if ge(lo, t) else F if lt(hi, t) else U
        if op == ">":
            return P if gt(lo, t) else F if le(hi, t) else U
        if op == "<=":
            return P if le(hi, t) else F if gt(lo, t) else U
        if op == "<":
            return P if lt(hi, t) else F if ge(lo, t) else U
        if op == "=":
            if point:
                return P if close(lo, t, rel_tol) else F
            return F if (lt(t, lo) or gt(t, hi)) else U
        # "!="
        if point:
            return F if close(lo, t, rel_tol) else P
        return P if (lt(t, lo) or gt(t, hi)) else U
    # categorical / textual
    ev_set = set(ev.values)
    if op == "contains":
        if view.value is None:
            return U
        needle = str(view.value).strip().lower()
        return P if needle in ev.text or any(needle in v for v in ev.values) else F
    bs = buyer_set(view)
    if bs is None or not ev_set:
        return U
    hit = bool(ev_set & set(bs))
    if op in ("in", "="):
        if hit:
            return P
        # numeric buyer target vs textual evidence: cannot call it a contradiction
        if op == "=" and target_number(view) is not None and not all(parse_number_unit(v) for v in ev_set):
            return U
        # Defect 6b: FAIL only for a different SPECIFIC value.  Generic / boolean-ish evidence ("Yes", "Customized",
        # "Standard") says nothing about the specific aspect required; a generic requirement ("as per buyer's
        # requirements") is not contradicted by anything.
        disp = [str(d).strip().lower() for d in ev.display]
        req0 = [str(a).strip().lower() for a in (view.allowed if view.allowed is not None
                                                 else _split_list(view.value or ""))]
        if req0 and all(r in _BOOLISH for r in req0) and disp and all(d in _BOOLISH for d in disp):
            return F                                   # Yes vs No: a genuine boolean contradiction
        if disp and all(_is_generic_text(d) for d in disp):
            return U
        if less_specific(view, ev):
            return U
        req = [str(a) for a in (view.allowed if view.allowed is not None else _split_list(view.value or ""))]
        if req and all(_is_generic_text(r) for r in req) and not all(d in _BOOLISH for d in disp):
            return U
        return F
    if op in ("not_in", "!="):
        return F if hit else P
    return U


def relabel(view: ConstraintView, **kw) -> ConstraintView:
    return replace(view, **kw)
