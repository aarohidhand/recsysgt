"""Deterministic scenario calibration (D050), no LLM.

For each tightness candidate (a state of the deterministic tightening walk whose initial verified-feasible count is
below ``negotiation.min_shortlist``) the negotiation is simulated with ``gtgen.matching.simulate_negotiation`` (the
SAME D053 front and buyer as the dialogue), ``top1_match_original`` is computed on the final state, and the first
candidate whose result lies inside the persona tier's band (``recommendation.tier_targets``) is chosen.  If none does,
the closest candidate (distance to the band, earlier step on ties) is kept and the caller adds
``recommendation.uncalibrated_flag``.  OWNER: data-steward.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence


def tier_band(settings: Mapping, tier: Any) -> Optional[tuple[float, float]]:
    tt = ((settings.get("recommendation") or {}).get("tier_targets")) or {}
    key = getattr(tier, "value", tier)
    if key not in tt:
        return None
    lo, hi = tt[key]
    return float(lo), float(hi)


def band_distance(value: Optional[float], band: tuple[float, float]) -> float:
    if value is None:
        return float("inf")
    lo, hi = band
    return 0.0 if lo <= value <= hi else (lo - value if value < lo else value - hi)


def top1_match_original(initial_constraints, final_constraints, candidates, ctx, *, quantity: float,
                        budget_inr: Optional[float], weights: Optional[Mapping[str, float]],
                        buyer_city: Optional[str], settings: Mapping, min_shortlist: int
                        ) -> tuple[Optional[str], Optional[float], str]:
    """(top1 product id, its match_original over the ORIGINAL negotiated constraints, outcome).

    Mirrors ``dialogue.assemble``: the truth ranking is built on the FINAL constraints; RECOMMENDED when the final
    feasible count reaches ``min_shortlist`` AND at least that many are VERIFIED (D075/D081 -- a scored
    PASS/PENDING_VERIFICATION on a negotiated spec constraint, not merely unblocked; top1 = rank 1), otherwise
    BEST_AVAILABLE (top1 = first best_available item). Before D081 this used the plain role-aware feasible count,
    so calibration could predict RECOMMENDED (and a multi-round negotiation) for a scenario the live negotiator
    always resolves as BEST_AVAILABLE/``nothing_verified`` on its very first turn (D076)."""
    from gtgen.matching import best_available, feasible_product_ids, hardness_map, match_original, verified_feasible_ids
    from gtgen.ranking.matrix import build_ranking_matrix

    cands = list(candidates)
    icells = ctx.cells(initial_constraints, cands)
    fcells = ctx.cells(final_constraints, cands)
    active = [c for c in final_constraints if c.framing_decision.value not in ("drop", "merge")]
    n_feas_ids = feasible_product_ids(fcells, hardness_map(active)) if active else [c.product_id for c in cands]
    n_feas = len(verified_feasible_ids(n_feas_ids, final_constraints, fcells, ctx.roles))
    rows = build_ranking_matrix(final_constraints, cands, fcells, quantity=quantity, budget_inr=budget_inr,
                                weights=weights, buyer_city=buyer_city, settings=settings,
                                original_constraints=initial_constraints, original_cells=icells)
    if not rows:
        return None, None, "BEST_AVAILABLE"
    mo = match_original(initial_constraints, icells, settings, product_ids=[c.product_id for c in cands])
    if n_feas >= min_shortlist:
        top1, outcome = rows[0].product_id, "RECOMMENDED"
    else:
        ba = best_available(rows, initial_constraints, icells, settings=settings, final_cells=fcells)
        top1 = ba.items[0].product_id if ba.items else rows[0].product_id
        outcome = "BEST_AVAILABLE"
    return top1, mo.get(top1), outcome


def choose(results: Sequence[Optional[float]], band: tuple[float, float]) -> tuple[int, bool]:
    """(index of the chosen candidate, in_band).  First in-band wins; else the closest (earlier on ties)."""
    best_i, best_d = 0, float("inf")
    for i, v in enumerate(results):
        d = band_distance(v, band)
        if d == 0.0:
            return i, True
        if d < best_d:
            best_i, best_d = i, d
    return best_i, False


def loosest_feasible(cells, hardness_by_ref: Mapping[str, Any]) -> int:
    """Number of products that satisfy every scored HARD constraint (no scored HARD FAIL/UNKNOWN) when ALL SOFT terms are
    relaxed away: the most a negotiation can ever reach (HARD constraints are never relaxed, rule 6).  It includes the
    products that are already feasible.  Pure."""
    from gtgen.contracts import Compliance, Hardness
    bad: set = set()
    seen: set = set()
    for c in cells:
        if not c.scored:
            continue
        seen.add(c.product_id)
        h = hardness_by_ref.get(c.constraint_ref)
        h = h if isinstance(h, Hardness) else (Hardness(h) if h is not None else None)
        if h == Hardness.HARD and c.status in (Compliance.FAIL, Compliance.UNKNOWN):
            bad.add(c.product_id)
    return len(seen - bad)


def unsatisfiable_blockers(cells, hardness_by_ref: Mapping[str, Any], min_shortlist: int) -> list[str]:
    """Scored HARD constraints whose removal alone would give >= ``min_shortlist`` products satisfying every other scored
    HARD constraint (audit aid for ``loosest_feasible < min_shortlist``; joint infeasibility).  Empty when no single
    constraint is a sufficient blocker (several together block).  Sorted by gain desc then id.  Pure."""
    from gtgen.contracts import Compliance, Hardness
    bad_by: dict[str, set] = {}          # constraint -> products failing it
    seen: set = set()
    for c in cells:
        if not c.scored:
            continue
        seen.add(c.product_id)
        h = hardness_by_ref.get(c.constraint_ref)
        h = h if isinstance(h, Hardness) else (Hardness(h) if h is not None else None)
        bad_by.setdefault(c.constraint_ref, set())
        if h == Hardness.HARD and c.status in (Compliance.FAIL, Compliance.UNKNOWN):
            bad_by[c.constraint_ref].add(c.product_id)
    hard_refs = [r for r in bad_by if (lambda h: (h if isinstance(h, Hardness) else
                                                  (Hardness(h) if h is not None else None)))(hardness_by_ref.get(r)) == Hardness.HARD]
    gains = []
    for r in hard_refs:
        bad_other = set().union(*[bad_by[o] for o in hard_refs if o != r]) if len(hard_refs) > 1 else set()
        n = len(seen - bad_other)
        if n >= int(min_shortlist):
            gains.append((-n, r))
    return [r for _, r in sorted(gains)]


def calibrate(points: Sequence[Mapping], evaluate: Callable[[Mapping], Mapping], band: tuple[float, float],
              tier: str) -> tuple[int, dict]:
    """``points`` = tightness candidates (dicts with ``step``/``count``); ``evaluate(point)`` -> dict with
    ``top1_match_original`` (or ``error``).  Returns (chosen index, calibration meta)."""
    tried = []
    for p in points:
        r = dict(evaluate(p))
        r.update({"step": p["step"], "initial_feasible": p["count"]})
        tried.append(r)
    # audit defect 5 / D070: a candidate is usable only if a negotiation path exists (evaluate sets ``path_exists``)
    usable = [i for i, t in enumerate(tried) if t.get("path_exists", True)]
    unsat = not usable
    pool = usable or list(range(len(tried)))
    j, ok = choose([tried[i].get("top1_match_original") for i in pool], band)
    idx = pool[j]
    ok = bool(ok) and not unsat
    meta = {"tier": tier, "band": list(band), "n_candidates": len(tried), "candidates": tried,
            "chosen_step": tried[idx]["step"], "chosen_top1_match_original": tried[idx].get("top1_match_original"),
            "in_band": ok, "unsatisfiable": unsat, "calibration_assumes_perfect_beliefs": True, "distance": band_distance(tried[idx].get("top1_match_original"), band)}
    return idx, meta


def scenario_validity(n_negotiated_product: int, initial_feasible: int, loosest: int, soft_only_blocked: int,
                      min_shortlist: int) -> dict:
    ms = int(min_shortlist)
    reasons: list[str] = []
    no_product = int(n_negotiated_product) < 1
    loosest_below = int(loosest) < ms
    no_soft_path = int(initial_feasible) < ms and (
        int(soft_only_blocked) < 1 or int(initial_feasible) + int(soft_only_blocked) < ms)
    # D098 (2026-11-04) + D101 (2026-11-04): none of these reasons block negotiation any more.
    # The negotiator always recommends something (D050: BEST_AVAILABLE on a synthetic-term-only
    # scenario, same as any other exhausted-relaxation scenario); they stay informational, feeding
    # diagnostics/unsatisfiable_blockers, never an invalid_scenario skip.
    return {"valid": not reasons, "reasons": reasons,
            "checks": {"negotiated_product_constraints": int(n_negotiated_product),
                       "initial_feasible": int(initial_feasible), "loosest_feasible": int(loosest),
                       "soft_only_blocked": int(soft_only_blocked), "min_shortlist": ms,
                       "no_negotiated_product_constraint": no_product,
                       "loosest_below_min_shortlist": loosest_below,
                       "no_soft_relaxation_path": no_soft_path}}


def invalid_scenario_skip(valid: bool, reasons: Sequence[str], settings: Mapping) -> Optional[str]:
    """``'invalid_scenario:<reason>[,<reason>]'`` when ``scenario.require_valid_for_negotiation`` is true and the
    scenario is not valid, else None (the caller records the skip)."""
    if bool(((settings.get("scenario") or {}).get("require_valid_for_negotiation", False))) and not valid:
        return "invalid_scenario:" + ",".join(reasons)
    return None
