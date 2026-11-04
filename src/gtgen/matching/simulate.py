"""D053 calibration simulator: runs the SAME loop as the dialogue (re-derive state -> need tables + joint front ->
reference menu point -> ``buyer_decide`` -> ``apply_decision``) without any LLM.  Pure and deterministic given
``seed``.  The caller (calibration) computes ``top1_match_original`` etc. from ``SimulationResult.final_constraints``
with the ranking/recommend modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from gtgen.contracts import Action, CandidateView, RelaxationAnalysisRow
from gtgen.matching.bounds import RelaxState, apply_decision, baseline_levels
from gtgen.matching.buyer_decision import (
    BuyerDecision,
    BuyerParams,
    ProposalItem,
    buyer_decide,
    buyer_rng,
)
from gtgen.matching.compliance import verified_feasible_ids
from gtgen.matching.evidence import ConstraintT
from gtgen.matching.front import Menu, build_menu
from gtgen.matching.gain import EvalContext, feasible_ids, feasible_set
from gtgen.matching.rules import RelaxConfig, RelaxRule, apply_levels, build_rules


@dataclass(frozen=True)
class SimulationResult:
    state: RelaxState
    levels: Mapping[str, int]
    final_constraints: tuple[ConstraintT, ...]     # originals with accepted levels applied
    rows: tuple[RelaxationAnalysisRow, ...]
    decisions: tuple[BuyerDecision, ...]
    stop_reason: str                                # shortlist | nothing_verified | no_options | patience | max_rounds
    feasible_initial: int
    feasible_final: int


def proposal_items(menu_point, rules: Mapping[str, RelaxRule], base: Mapping[str, int]) -> list[ProposalItem]:
    return [ProposalItem(ref=c["ref"], level=c["level"], max_level=rules[c["ref"]].top,
                         hardness=rules[c["ref"]].hardness, category=rules[c["ref"]].category,
                         doc_proof=False, current_level=base[c["ref"]]) for c in menu_point.changes]


def simulate_negotiation(constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView], ctx: EvalContext,
                         cfg: RelaxConfig, params: BuyerParams, *, seed: object, min_shortlist: int = 3,
                         max_rounds: int = 20, unit: str = "supplier",
                         overrides: Optional[Mapping[str, Mapping]] = None) -> SimulationResult:
    """Reference negotiator (always proposes the cheapest menu point) against ``buyer_decide``.

    Stops when the feasible PRODUCT count reaches ``min_shortlist`` AND at least that many are VERIFIED
    (``shortlist``; D075/D081 -- otherwise ``nothing_verified``, since relaxing a SOFT term further can never
    produce catalogue evidence for a HARD spec constraint that has none, so continuing the loop would only predict
    rounds the live negotiator will never run), the menu is empty (no_options), ``params.patience_rounds``
    consecutive rounds brought no YES (patience), or ``max_rounds``."""
    rng = buyer_rng(seed)
    rules = build_rules(constraints, cfg, roles=ctx.roles, overrides=overrides, candidates=candidates)
    by = {r.ref: r for r in rules}
    state = RelaxState()
    feas0 = len(feasible_set(constraints, candidates, ctx))
    rows: list[RelaxationAnalysisRow] = []
    decisions: list[BuyerDecision] = []
    stop, dry = "max_rounds", 0
    for it in range(max_rounds):
        lv = baseline_levels(rules, state.bounds)
        cons_now = apply_levels(constraints, rules, lv) if rules and lv else list(constraints)
        cells_now = ctx.cells(cons_now, candidates)
        now = feasible_ids(cons_now, candidates, cells_now)
        if len(now) >= min_shortlist:
            verified_now = verified_feasible_ids(now, constraints, cells_now, ctx.roles)
            stop = "shortlist" if len(verified_now) >= min_shortlist else "nothing_verified"
            break
        menu: Menu = build_menu(constraints, candidates, ctx, cfg, state.bounds, unit=unit,
                                diagnostic_used=state.diagnostic_used, overrides=overrides)
        ref_pt = menu.reference()
        if ref_pt is None:
            stop = "no_options"
            break
        dec = buyer_decide(proposal_items(ref_pt, by, lv), params, rng,
                           accepted_rules=[r for r, b in state.bounds.items() if "fixed" in b],
                           pool=len({c.supplier_id for c in candidates}), diagnostic=menu.diagnostic)
        res = apply_decision(state, rules, ref_pt.proposal(), dec.outcomes(), turn=it, diagnostic=menu.diagnostic)
        if not res.ok:
            raise RuntimeError(f"invalid transition: {res.errors}")
        new_lv = baseline_levels(rules, res.state.bounds)
        after = feasible_set(constraints, candidates, ctx, rules=rules, levels=new_lv)
        rows.append(RelaxationAnalysisRow(
            turn_idx=it, iteration=it, front_points=[p.to_dict() for p in menu.points], chosen_point=ref_pt.to_dict(),
            chosen_action="PROPOSE_RELAXATION", bounds_before_truth={k: dict(v) for k, v in state.bounds.items()},
            bounds_after_truth={k: dict(v) for k, v in res.state.bounds.items()}, verified_before_truth=len(now),
            verified_after_truth=len(after),
            buyer_decision={d.ref: {"action": d.action.value, "level": d.level, "reason": d.reason}
                            for d in dec.items}, agrees_with_reference=True))
        decisions.append(dec)
        yes = any(d.action == Action.YES for d in dec.items)
        state = res.state
        dry = 0 if yes else dry + 1
        if dry >= params.patience_rounds:
            stop = "patience"
            break
    lv = baseline_levels(rules, state.bounds)
    final = apply_levels(constraints, rules, lv)
    return SimulationResult(state=state, levels=lv, final_constraints=tuple(final), rows=tuple(rows),
                            decisions=tuple(decisions), stop_reason=stop, feasible_initial=feas0,
                            feasible_final=len(feasible_set(constraints, candidates, ctx, rules=rules, levels=lv)))
