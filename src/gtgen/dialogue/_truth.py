"""Truth-side relaxation state (D052/D053/D056). Buyer-side module: the Negotiator never imports it.

``TruthState`` holds the ORIGINAL truth constraints (immutable), the relaxable rules built from them, the
``RelaxState`` (bounds, append-only Change records, verification list) and the derived current view: levels, relaxed
constraints, compliance cells and the feasible product ids. ``apply`` feeds a buyer decision through
``matching.apply_decision`` and re-derives everything (the loop recomputes need tables and the front from the
bounds every turn; nothing is cached across turns).
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence

from gtgen.contracts import BuyerConstraint, CandidateView, ComplianceCell
from gtgen.matching import (
    ApplyResult, EvalContext, ItemOutcome, RelaxConfig, RelaxRule, RelaxState, apply_decision, apply_levels,
    baseline_levels, build_rules, feasible_ids,
)

from ._relax import bound_snapshot


class TruthState:
    def __init__(self, constraints: Sequence[BuyerConstraint], candidates: Sequence[CandidateView],
                 ctx: EvalContext, cfg: RelaxConfig) -> None:
        self.originals: list[BuyerConstraint] = list(constraints)
        self.candidates = list(candidates)
        self.ctx = ctx
        self.cfg = cfg
        self.rules: list[RelaxRule] = build_rules(self.originals, cfg, roles=ctx.roles,
                                                  candidates=self.candidates)
        self.by_ref: dict[str, RelaxRule] = {r.ref: r for r in self.rules}
        self.state = RelaxState()
        self.levels: dict[str, int] = {}
        self.constraints: list[BuyerConstraint] = []
        self.cells: list[ComplianceCell] = []
        self.feasible: list[str] = []
        self._refresh()
        self.initial_constraints = list(self.constraints)
        self.initial_cells = list(self.cells)

    def _refresh(self) -> None:
        self.levels = baseline_levels(self.rules, self.state.bounds)
        self.constraints = apply_levels(self.originals, self.rules, self.levels)        # type: ignore[assignment]
        self.cells = self.ctx.cells(self.constraints, self.candidates)
        self.feasible = feasible_ids(self.constraints, self.candidates, self.cells)

    @property
    def bounds(self) -> dict[str, dict[str, int]]:
        return bound_snapshot(self.state.bounds)

    def apply(self, proposal: Mapping[str, int], outcomes: Sequence[ItemOutcome], *, turn: int,
              justification: Optional[Mapping] = None) -> ApplyResult:
        res = apply_decision(self.state, self.rules, proposal, outcomes, turn=turn, justification=justification)
        if res.ok:
            self.state = res.state
            self._refresh()
        return res
