"""Scenario construction (D046, D048, D049): evidence coverage, scenario roles, commercial terms.

OWNER: data-steward. Pure, deterministic functions. Every threshold and list comes from
``settings["scenario"]`` (and a few ``ranking`` / ``negotiation`` keys); a missing key raises
``KeyError`` - there is no in-code fallback.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
from collections import Counter
from typing import Any, Callable, Mapping, Optional, Sequence

import numpy as np

from gtgen.contracts import (
    BuyerConstraint,
    Compliance,
    DocClaim,
    FramingDecision,
    Hardness,
    NormalisedValue,
    ScenarioRole,
)

TERM_ORDER = ["budget_inr", "qty_vs_moq", "preferred_states"]   # D070: rating / tenure are ranking-only
_OPERATOR = {"budget_inr": "<=", "qty_vs_moq": "<=", "preferred_states": "in"}
_SCOPE = {"budget_inr": "product", "qty_vs_moq": "product", "preferred_states": "supplier"}
_UNIT = {"budget_inr": "INR", "qty_vs_moq": "fraction", "preferred_states": None}
_INACTIVE = (FramingDecision.DROP, FramingDecision.MERGE)


def _sc(settings: Mapping) -> Mapping:
    return settings["scenario"]


def _hardness(c: BuyerConstraint) -> Hardness:
    return c.hardness if isinstance(c.hardness, Hardness) else Hardness(c.hardness)


# ------------------------------------------------------------------ catalogue fields
def _field_present(term: str, cand) -> bool:
    if term == "budget_inr":
        return cand.price is not None
    if term == "qty_vs_moq":
        return cand.moq is not None
    if term == "preferred_states":
        return bool(cand.state and str(cand.state).strip())
    raise ValueError(f"unknown synthetic commercial term {term!r}")


def field_coverage(candidates: Sequence, settings: Mapping) -> dict[str, float]:
    """Share of candidates that HAVE the catalogue field behind each configured commercial term."""
    terms = _sc(settings)["commercial_terms"]["terms"]
    n = len(candidates)
    return {t: (sum(_field_present(t, c) for c in candidates) / n if n else 0.0) for t in terms}


# ------------------------------------------------------------------ 1. coverage
def compute_coverage(constraints: Sequence[BuyerConstraint], candidates: Sequence, alignments,
                     *, normalised: Optional[Mapping[tuple[str, str], NormalisedValue]] = None,
                     doc_claims: Optional[Mapping[tuple[str, str], DocClaim]] = None,
                     settings: Mapping) -> dict[str, float]:
    """constraint_id -> share of ALL pool candidates whose evidence is not UNKNOWN (active constraints only)."""
    from gtgen.matching import compliance_matrix
    n = len(candidates)
    active = [c for c in constraints if c.framing_decision not in _INACTIVE]
    out: dict[str, float] = {}
    gem = [c for c in active if c.provenance != "synthetic_commercial"]
    for c in active:
        if c.provenance == "synthetic_commercial":
            out[c.constraint_id] = (sum(_field_present(c.attribute, k) for k in candidates) / n) if n else 0.0
    if gem:
        al = dict(alignments) if isinstance(alignments, Mapping) else {a.buyer_attribute: a for a in alignments}
        kw = dict(unverified_factor=settings["ranking"]["unverified_text_factor"],
                  rel_tol=settings["negotiation"]["bundle_search"]["numeric_eq_rel_tolerance"])
        cells = compliance_matrix(gem, candidates, al, normalised, **kw) if n else []
        known: Counter = Counter()
        for cell in cells:
            if cell.status != Compliance.UNKNOWN:
                known[cell.constraint_ref] += 1
        for c in gem:
            if c.is_doc_proof and doc_claims is not None:
                hit = sum(1 for k in candidates
                          if (d := doc_claims.get((k.product_id, c.constraint_id))) is not None and d.claims)
                out[c.constraint_id] = hit / n if n else 0.0
            else:
                out[c.constraint_id] = known[c.constraint_id] / n if n else 0.0
    return out


def load_cached_normalised(bid_id: str, catalogue, out_dir: str = "out/coverage_llm") -> dict:
    """Rebuild {(product_id, buyer_attribute): NormalisedValue} (grounded values only) from the cached
    ``<slug>_requests.json`` + ``<slug>_normalised.json``. Empty dict if either file is absent."""
    from gtgen.data.coverage_llm import is_grounded, slug
    rp = os.path.join(out_dir, f"{slug(bid_id)}_requests.json")
    sp = os.path.join(out_dir, f"{slug(bid_id)}_normalised.json")
    if not (os.path.isfile(rp) and os.path.isfile(sp)):
        return {}
    with open(rp, encoding="utf-8") as f:
        req = json.load(f)
    with open(sp, encoding="utf-8") as f:
        res = json.load(f)
    pool = {c.product_id for c in catalogue.candidates}
    results = res.get("results", {})
    nv: dict = {}
    for r in req.get("requests", []):
        o = results.get(r["rid"])
        if o is None or r["product_id"] not in pool:
            continue
        if o.get("value") is None or not str(o["value"]).strip():
            continue
        if not o.get("grounded") or not is_grounded(o["value"], r["text"]):
            continue
        nv[(r["product_id"], r["attribute"])] = NormalisedValue(
            source_text=r["text"], value=str(o["value"]).strip(), unit=o.get("unit"),
            confidence=float(o["confidence"]) if o.get("confidence") is not None else 0.5,
            llm_call_id=o.get("llm_call_id", ""))
    return nv


# ------------------------------------------------------------------ 2. roles
def assign_roles(constraints: Sequence[BuyerConstraint], coverage: Mapping[str, float],
                 settings: Mapping) -> list[BuyerConstraint]:
    """Scenario role per constraint (D048, amended by user decision Q3 of 2026-10-29).

    DROP -> DROPPED; MERGE members -> None (the merged constraint carries the role); synthetic commercial terms
    -> NEGOTIATED regardless of coverage; every other constraint, HARD *and* SOFT, is NEGOTIATED when
    ``evidence_coverage >= scenario.negotiated_min_coverage`` and VERIFICATION_ONLY below it (one cutoff for both
    hardnesses). A missing coverage entry raises for any such constraint. Coverage is stored on the result.
    """
    thr = float(_sc(settings)["negotiated_min_coverage"])
    out: list[BuyerConstraint] = []
    for c in constraints:
        cov = coverage.get(c.constraint_id)
        if c.framing_decision == FramingDecision.DROP:
            out.append(c.model_copy(update={"scenario_role": ScenarioRole.DROPPED, "evidence_coverage": cov}))
        elif c.framing_decision == FramingDecision.MERGE:
            out.append(c.model_copy(update={"scenario_role": None, "evidence_coverage": None}))
        elif c.provenance == "synthetic_commercial":
            out.append(c.model_copy(update={"scenario_role": ScenarioRole.NEGOTIATED, "evidence_coverage": cov}))
        else:
            if cov is None:
                raise ValueError(f"no coverage value for {_hardness(c).value} constraint {c.constraint_id}")
            role = ScenarioRole.NEGOTIATED if cov >= thr else ScenarioRole.VERIFICATION_ONLY
            out.append(c.model_copy(update={"scenario_role": role, "evidence_coverage": cov,
                                            "role_reason": None if cov >= thr else "low_coverage"}))
    return out


def zero_pass_enabled(settings: Mapping) -> bool:
    return bool(_sc(settings).get("demote_zero_pass", True))


def demote_zero_pass(constraints: Sequence[BuyerConstraint], pass_counts: Mapping[str, int],
                     settings: Mapping) -> tuple[list[BuyerConstraint], dict]:
    """D069 zero-PASS demotion, applied after ``assign_roles`` (D048) and before tightness/calibration.

    A NEGOTIATED, non-synthetic HARD constraint whose PASS count over the FULL candidate set is exactly 0 becomes
    VERIFICATION_ONLY with ``role_reason = "zero_pass"``. ``pass_counts`` = constraint_id -> number of candidates whose
    truth-side cell is PASS or PENDING_VERIFICATION. Constraints with >= 1 PASS are untouched (D048), as are synthetic
    terms, DROP/MERGE members, SOFT constraints (not demoted: whether a SOFT constraint is satisfiable at its loosest
    relaxation level is decided by the relaxation loop, D053), and anything already verification_only (low_coverage).
    Returns (constraints, meta); a disabled flag returns the input unchanged with ``enabled: False``."""
    enabled = zero_pass_enabled(settings)
    out: list[BuyerConstraint] = []
    demoted: list[str] = []
    for c in constraints:
        if (enabled and c.scenario_role == ScenarioRole.NEGOTIATED and c.provenance != "synthetic_commercial"
                and c.framing_decision not in (FramingDecision.DROP, FramingDecision.MERGE)
                and _hardness(c) == Hardness.HARD and int(pass_counts.get(c.constraint_id, 0)) == 0):
            out.append(c.model_copy(update={"scenario_role": ScenarioRole.VERIFICATION_ONLY,
                                            "role_reason": "zero_pass"}))
            demoted.append(c.constraint_id)
        else:
            out.append(c)
    reasons = Counter(c.role_reason for c in out if c.scenario_role == ScenarioRole.VERIFICATION_ONLY)
    meta = {"enabled": enabled, "demoted_zero_pass": demoted,
            "pass_counts_demoted": {cid: int(pass_counts.get(cid, 0)) for cid in demoted},
            "role_reason_counts": {"low_coverage": int(reasons.get("low_coverage", 0)),
                                   "zero_pass": int(reasons.get("zero_pass", 0))},
            "soft_not_demoted": "SOFT constraints are left to the relaxation loop (D053)"}
    return out, meta


# ------------------------------------------------------------------ 2b. duplicate / conflicting attributes
_UNIT_BASE = {  # unit token -> (dimension, factor to the base unit)
    "mg": ("mass", 1e-3), "g": ("mass", 1.0), "gm": ("mass", 1.0), "gms": ("mass", 1.0), "gram": ("mass", 1.0),
    "grams": ("mass", 1.0), "kg": ("mass", 1e3), "kgs": ("mass", 1e3), "kilogram": ("mass", 1e3),
    "mm": ("length", 1e-3), "cm": ("length", 1e-2), "m": ("length", 1.0), "meter": ("length", 1.0),
    "metre": ("length", 1.0), "ml": ("volume", 1e-3), "l": ("volume", 1.0), "litre": ("volume", 1.0),
}


def attribute_key(attribute: str) -> str:
    """Normalised attribute name: lower case, spaces/dashes -> '_', and the extractor's duplicate suffix
    ``_alt``/``_altN`` removed (the data marks a second row for the same attribute that way)."""
    a = re.sub(r"[\s\-]+", "_", str(attribute).strip().lower())
    return re.sub(r"_alt\d*$", "", a)


def _as_base(c: BuyerConstraint) -> Optional[tuple[str, float]]:
    """(dimension, value in the base unit) for a numeric constraint; None when not numeric. A unit that is not in the
    table (e.g. gsm) is its own dimension, so it is never silently compared to a plain mass."""
    try:
        v = float(str(c.buyer_value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    u = (c.unit or "").strip().lower()
    dim, f = _UNIT_BASE.get(u, (f"unit:{u}", 1.0))
    return dim, v * f


def _interval(c: BuyerConstraint) -> Optional[tuple[str, float, float]]:
    nb = _as_base(c)
    if nb is None:
        return None
    dim, v = nb
    op = (c.operator or "=").strip()
    if op in (">=", ">"):
        return dim, v, math.inf
    if op in ("<=", "<"):
        return dim, -math.inf, v
    return dim, v, v


def value_relation(a: BuyerConstraint, b: BuyerConstraint, rel_tol: float = 0.01) -> str:
    """'same' (identical value after unit conversion / case-insensitive text), 'compatible' (numeric bounds whose
    allowed ranges intersect, e.g. '>= 3 kW' and '>= 100 kW'), or 'conflict' (no value satisfies both)."""
    ia, ib = _interval(a), _interval(b)
    if ia is not None and ib is not None:
        if ia[0] != ib[0]:
            return "conflict"                       # different dimension / unit family (e.g. gsm vs kg)
        if math.isclose(ia[1], ib[1], rel_tol=rel_tol, abs_tol=1e-12) and \
                math.isclose(ia[2], ib[2], rel_tol=rel_tol, abs_tol=1e-12):
            return "same"
        lo, hi = max(ia[1], ib[1]), min(ia[2], ib[2])
        tol = rel_tol * max(abs(x) for x in (lo, hi, 1e-12) if math.isfinite(x))
        return "compatible" if lo <= hi + tol else "conflict"
    if ia is not None or ib is not None:
        return "conflict"
    norm = lambda x: re.sub(r"\s+", " ", str(x or "").strip().lower())  # noqa: E731
    return "same" if (norm(a.buyer_value) == norm(b.buyer_value) and norm(a.unit) == norm(b.unit)) else "conflict"


def _voiced(c: BuyerConstraint) -> bool:
    return (c.provenance != "synthetic_commercial" and c.framing_decision not in _INACTIVE
            and c.scenario_role != ScenarioRole.DROPPED)


def find_conflicts(constraints: Sequence[BuyerConstraint]) -> dict[str, list[str]]:
    """Validator: normalised attribute -> constraint ids, for every attribute shared by two or more voiced constraints
    (not DROP/MERGE/DROPPED, not synthetic) whose values are incompatible. Empty dict = clean brief."""
    by: dict[str, list[BuyerConstraint]] = {}
    for c in constraints:
        if _voiced(c):
            by.setdefault(attribute_key(c.attribute), []).append(c)
    out: dict[str, list[str]] = {}
    for k, g in by.items():
        if len(g) > 1 and any(value_relation(g[0], x) == "conflict" for x in g[1:]):
            out[k] = [x.constraint_id for x in g]
    return out


def reconcile_attributes(constraints: Sequence[BuyerConstraint], coverage: Mapping[str, float]
                         ) -> tuple[list[BuyerConstraint], list[dict], list[str]]:
    """One constraint per normalised attribute (run after ``compute_coverage``, before ``assign_roles``).

    Voiced constraints sharing ``attribute_key`` are reconciled: the winner is the one with the higher evidence
    coverage, then the one with an evidence anchor (exactly representable), then HARD over SOFT, then input order.
    Every loser gets ``framing_decision = DROP`` with a ``framing_reason`` naming the winner (``assign_roles`` then makes
    it DROPPED, so it is neither voiced nor scored).  A pair stating the same value is an ``alternate`` (no flag); a pair
    with incompatible values is a ``conflict`` and adds ``conflicting_constraints:<attribute>`` to the returned qa flags.
    Returns (constraints, per-group records for ``scenario_meta``, qa flags)."""
    groups: dict[str, list[int]] = {}
    for i, c in enumerate(constraints):
        if _voiced(c):
            groups.setdefault(attribute_key(c.attribute), []).append(i)
    out = list(constraints)
    records: list[dict] = []
    flags: list[str] = []
    for key, idx in groups.items():
        if len(idx) < 2:
            continue
        ranked = sorted(idx, key=lambda i: (-float(coverage.get(constraints[i].constraint_id) or 0.0),
                                            0 if constraints[i].evidence_anchor else 1,
                                            0 if _hardness(constraints[i]) == Hardness.HARD else 1, i))
        w = constraints[ranked[0]]
        dropped = []
        kind = None
        for i in ranked[1:]:
            c = constraints[i]
            rel = value_relation(w, c)
            if rel == "compatible":
                continue                       # e.g. '>= 3 kW' and '>= 100 kW': both bounds can be honoured
            same = rel == "same"
            kind = "conflict" if (kind == "conflict" or not same) else "alternate"
            kept_v = f"{w.buyer_value} {w.unit or ''}".strip()
            drop_v = f"{c.buyer_value} {c.unit or ''}".strip()
            why = (f"duplicate_attribute:{key}: {'alternate representation' if same else 'conflicting value'} of "
                   f"{w.constraint_id} (kept {kept_v}, dropped {drop_v})")
            out[i] = c.model_copy(update={"framing_decision": FramingDecision.DROP, "framing_reason": why})
            dropped.append({"constraint_id": c.constraint_id, "attribute": c.attribute, "buyer_value": c.buyer_value,
                            "unit": c.unit, "evidence_coverage": coverage.get(c.constraint_id), "relation": rel})
        if not dropped:
            continue
        records.append({"attribute": key, "kind": kind, "kept": w.constraint_id, "kept_value": w.buyer_value,
                        "kept_unit": w.unit, "kept_evidence_coverage": coverage.get(w.constraint_id),
                        "dropped": dropped})
        if kind == "conflict":
            flags.append(f"conflicting_constraints:{key}")
    return out, records, flags


def coverage_meta(coverage: Mapping[str, float], constraints: Sequence[BuyerConstraint], n_pool: int,
                  n_normalised_candidates: int, settings: Mapping) -> dict:
    roles = Counter(c.scenario_role.value for c in constraints if c.scenario_role is not None)
    return {
        "method": "structured+cached_grounded_normalised" if n_normalised_candidates > 0 else "structured_only",
        "n_pool": int(n_pool),
        "n_candidates_with_normalised_evidence": int(n_normalised_candidates),
        "negotiated_min_coverage": float(_sc(settings)["negotiated_min_coverage"]),
        "role_counts": {r.value: int(roles.get(r.value, 0)) for r in ScenarioRole},
        "n_constraints_with_coverage": len(coverage),
    }


# ------------------------------------------------------------------ 3. bands
def _numeric_band(values: list[float], unit: str, tight_end: str, pct: Sequence[float]) -> tuple[Optional[dict], Optional[str]]:
    if len(set(values)) < 2:
        return None, f"fewer than 2 distinct pool values ({len(set(values))})"
    lo, hi = (float(x) for x in np.percentile(values, [100.0 * p for p in pct]))
    if lo == hi:
        return None, "degenerate band (lo == hi at the configured percentiles)"
    return {"kind": "numeric", "lo": lo, "hi": hi, "unit": unit, "tight_end": tight_end}, None


def commercial_bands(catalogue, quantity: float, settings: Mapping) -> dict:
    ct = _sc(settings)["commercial_terms"]
    listed, pct = ct["terms"], ct["band_percentiles"]
    cands = list(catalogue.candidates)
    q = float(quantity)
    if q <= 0:
        raise ValueError("quantity must be positive")
    vals = {
        "budget_inr": ([c.price * q for c in cands if c.price is not None], "INR", "lo"),
        "qty_vs_moq": ([max(0.0, c.moq / q - 1.0) for c in cands if c.moq is not None], "fraction", "lo"),
    }
    terms: dict = {}
    omitted: dict = {}
    for t in TERM_ORDER:
        if t not in listed:
            omitted[t] = "not listed in scenario.commercial_terms.terms"
            continue
        if t == "preferred_states":
            cnt = Counter(str(c.state).strip() for c in cands if c.state and str(c.state).strip())
            if len(cnt) < 2:
                omitted[t] = f"fewer than 2 distinct states in the pool ({len(cnt)})"
                continue
            opts = [s for s, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))]
            terms[t] = {"kind": "set", "options": opts, "min_k": 1, "max_k": min(3, len(opts)),
                        "tight_end": "fewer"}
            continue
        v, unit, end = vals[t]
        band, why = _numeric_band([float(x) for x in v], unit, end, pct)
        if band is None:
            omitted[t] = why
        else:
            terms[t] = band
    return {"terms": terms, "omitted": omitted}


# ------------------------------------------------------------------ 4. synthetic constraints
def _inr(x: float) -> str:
    """Indian digit grouping, e.g. 450000 -> '4,50,000'."""
    s = str(int(round(x)))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def _g(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def build_commercial_constraints(bid_id: str, quantity: float, picks: Mapping[str, Any], settings: Mapping,
                                 *, field_coverage: Optional[Mapping[str, float]] = None) -> list[BuyerConstraint]:
    ct = _sc(settings)["commercial_terms"]
    cats = ct["category"]
    out: list[BuyerConstraint] = []
    for t in TERM_ORDER:
        if t not in picks:
            continue
        if t not in ct["terms"]:
            raise ValueError(f"term {t!r} is not listed in scenario.commercial_terms.terms")
        v = picks[t]
        allowed: Optional[list[str]] = None
        if t == "budget_inr":
            val, text = _g(float(v)), f"Our budget for the whole order is up to INR {_inr(float(v))}."
        elif t == "qty_vs_moq":
            cap = float(quantity) * (1.0 + float(v))
            val = _g(float(v))
            text = (f"The supplier's minimum order quantity must not exceed {_g(cap)} units, "
                    f"as we are ordering {_g(float(quantity))} units.")
        else:
            allowed = [str(s) for s in v]
            if not allowed:
                raise ValueError("preferred_states pick must be non-empty")
            val = ", ".join(allowed)
            text = f"We prefer suppliers based in {', '.join(allowed)}."
        out.append(BuyerConstraint(
            constraint_id=f"{bid_id}_S_{t}", bid_id=bid_id, golden_param=None, hardness=Hardness.SOFT,
            provenance="synthetic_commercial", scenario_role=ScenarioRole.NEGOTIATED,
            evidence_coverage=float(field_coverage[t]) if field_coverage and t in field_coverage else 1.0,
            attribute=t, buyer_value=val, operator=_OPERATOR[t], unit=_UNIT[t], allowed_values=allowed,
            constraint_category=cats[t], constraint_type="commercial", scope=_SCOPE[t],
            is_doc_proof=False, framing_decision=FramingDecision.KEEP, commercial_text=text))
    return out


# ------------------------------------------------------------------ 5/6. tightness
def tightness_target(persona_seed: int, settings: Mapping) -> int:
    lo, hi = (int(x) for x in _sc(settings)["commercial_terms"]["tightness_target"])
    if hi < lo:
        raise ValueError("scenario.commercial_terms.tightness_target must be [lo, hi] with lo <= hi")
    if hi >= int(settings["negotiation"]["min_shortlist"]):
        raise ValueError("tightness_target upper bound must be < negotiation.min_shortlist")
    return lo + int(persona_seed) % (hi - lo + 1)


def _step_term(t: str, picks: dict, spec: Mapping) -> bool:
    """Tighten one term in place; return False if it cannot move."""
    if spec["kind"] == "set":
        cur = list(picks[t])
        if len(cur) <= int(spec["min_k"]):
            return False
        rank = {s: i for i, s in enumerate(spec["options"])}
        worst = max(range(len(cur)), key=lambda i: (rank.get(cur[i], len(rank)), i))
        picks[t] = cur[:worst] + cur[worst + 1:]
        return True
    cur = float(picks[t])
    tight = float(spec["lo"] if spec["tight_end"] == "lo" else spec["hi"])
    if abs(tight - cur) < 1e-9:
        return False
    new = min(max(cur + 0.25 * (tight - cur), float(spec["lo"])), float(spec["hi"]))
    if new == cur:
        return False
    picks[t] = new
    return True


def tightness_floors(catalogue, quantity: float, settings: Mapping) -> dict[str, float]:
    """Lowest value a tightening may reach BELOW the band (D073).  Only the budget has a floor: the realism check
    ``budget`` (persona.realism.budget_catalogue_factor[0] x quantity x cheapest unit price) -> never below it.  {} when
    the pool has no prices or ``scenario.commercial_terms.snap_below_band`` is false."""
    ct = _sc(settings)["commercial_terms"]
    if not bool(ct.get("snap_below_band", True)):
        return {}
    prices = [float(c.price) for c in catalogue.candidates if c.price is not None and c.price > 0]
    if not prices or float(quantity) <= 0:
        return {}
    f_lo = float(((settings.get("persona") or {}).get("realism") or {}).get("budget_catalogue_factor", [0.5, 3.0])[0])
    return {"budget_inr": f_lo * float(quantity) * min(prices) * 1.001}


def _snap_states(cur: dict, bands: Mapping, floors: Optional[Mapping[str, float]], count_fn: Callable[[dict], int],
                 target: int, max_extra: int = 60, factor: float = 0.9) -> list[dict]:
    """D073 snap phase, entered when the geometric walk could not get the count below the shortlist (the walk moves 25% of
    the way to the band edge per step, and the band edge is the 20th percentile of the pool, so >= 20% of the pool still
    fits).  States visited, in order: (A) every term moved straight to its band edge; (B) budget lowered by ``factor`` per
    step below the band edge, down to ``floors['budget_inr']``, until the count is <= ``target``.  ``cur`` is mutated
    and the list holds {"picks", "count", "term"} per state.  Pure and deterministic."""
    out: list[dict] = []
    terms = [t for t in TERM_ORDER if t in cur and t in bands["terms"]]

    def push(t: str) -> int:
        n = int(count_fn(copy.deepcopy(cur)))
        out.append({"picks": copy.deepcopy(cur), "count": n, "term": t})
        return n

    for t in terms:                                           # (A)
        spec = bands["terms"][t]
        if spec["kind"] == "set":
            keep = int(spec["min_k"])
            if len(cur[t]) <= keep:
                continue
            rank = {x: i for i, x in enumerate(spec["options"])}
            cur[t] = sorted(cur[t], key=lambda x: (rank.get(x, len(rank)), x))[:keep]
        else:
            edge = float(spec["lo"] if spec["tight_end"] == "lo" else spec["hi"])
            if abs(float(cur[t]) - edge) < 1e-9:
                continue
            cur[t] = edge
        if push(t) <= target:
            return out
    for t in terms:                                           # (B)
        spec = bands["terms"][t]
        floor = (floors or {}).get(t)
        if floor is None or spec["kind"] == "set" or spec["tight_end"] != "lo":
            continue
        for _ in range(max_extra):
            v = float(cur[t])
            if v <= floor + 1e-9:
                break
            cur[t] = max(v * factor, float(floor))
            if push(t) <= target:
                return out
    return out


def tighten(picks: Mapping[str, Any], bands: Mapping, count_fn: Callable[[dict], int], target: int,
            min_shortlist: int, max_steps: int, floors: Optional[Mapping[str, float]] = None) -> tuple[dict, dict]:
    cur = copy.deepcopy(dict(picks))
    before = count_fn(copy.deepcopy(cur))
    count = before
    done: list[str] = []
    terms = [t for t in TERM_ORDER if t in cur and t in bands["terms"]]
    if count >= min_shortlist:
        i = 0
        stalled = 0
        steps = 0
        while steps < max_steps and count >= min_shortlist and terms and stalled < len(terms):
            t = terms[i % len(terms)]
            i += 1
            if not _step_term(t, cur, bands["terms"][t]):
                stalled += 1
                continue
            stalled = 0
            steps += 1
            done.append(t)
            count = count_fn(copy.deepcopy(cur))
    snap = []
    if count >= min_shortlist and floors is not None and terms:       # D073: the walk alone cannot reach the target
        snap = _snap_states(cur, bands, floors, count_fn, target)
        if snap:
            count = snap[-1]["count"]
            done += [x["term"] for x in snap]
    meta = {"target": int(target), "count_before": int(before), "count_after": int(count),
            "steps": len(done), "terms_tightened": done, "snap_steps": len(snap),
            "reached_below_min_shortlist": bool(count < min_shortlist),
            "target_reached": bool(count <= target)}
    return cur, meta


def tighten_trajectory(picks: Mapping[str, Any], bands: Mapping, count_fn: Callable[[dict], int],
                       max_steps: int, floors: Optional[Mapping[str, float]] = None, target: int = 0,
                       min_shortlist: Optional[int] = None) -> list[dict]:
    """Every state visited by the deterministic tightening walk (same term order and step rule as ``tighten``),
    CONTINUED past the point where the count first falls below the shortlist, up to ``max_steps`` steps.

    Entry k = {"step": k, "picks": <copy>, "count": <verified-feasible count>, "terms_tightened": [...]}.  Entry 0 is the
    untouched LLM picks.  Used by calibration (D050): the candidates are the entries whose count is below the
    shortlist.  D073: when ``floors`` and ``min_shortlist`` are given and NO visited state is below the shortlist, the
    snap phase (``_snap_states``) is appended.  Pure and deterministic."""
    cur = copy.deepcopy(dict(picks))
    done: list[str] = []
    out = [{"step": 0, "picks": copy.deepcopy(cur), "count": int(count_fn(copy.deepcopy(cur))), "terms_tightened": []}]
    terms = [t for t in TERM_ORDER if t in cur and t in bands["terms"]]
    i = stalled = 0
    while len(done) < max_steps and terms and stalled < len(terms):
        t = terms[i % len(terms)]
        i += 1
        if not _step_term(t, cur, bands["terms"][t]):
            stalled += 1
            continue
        stalled = 0
        done.append(t)
        out.append({"step": len(done), "picks": copy.deepcopy(cur), "count": int(count_fn(copy.deepcopy(cur))),
                    "terms_tightened": list(done)})
    if floors is not None and min_shortlist is not None and terms and all(o["count"] >= min_shortlist for o in out):
        for st in _snap_states(cur, bands, floors, count_fn, target):
            done.append(st["term"])
            out.append({"step": len(done), "picks": st["picks"], "count": st["count"], "terms_tightened": list(done),
                        "snap": True})
    return out
