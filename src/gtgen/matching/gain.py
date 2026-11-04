"""THE shared role-aware gain function (D056 / user item 1).

One counting path for ranking-side shortlists, calibration, the relaxation front/menu and gain reporting
(``truth_gain`` / ``negotiator_estimated_gain``):

    feasible set  = ``compliance.feasible`` over role-aware cells (verification_only cells are ``scored=False``
                    and never block; an observed FAIL on them blocks only if ``verification_only_fail_blocks``);
    gain          = |feasible after| - |feasible before|  (``gain_between``)

Both candidate (product) and supplier counts come out of the same ``GainResult``; ``RelaxationOption.truth_gain``
and ``negotiator_estimated_gain`` are the PRODUCT count (``GainResult.gain_products``), the front's staircase uses
the SUPPLIER count (spec 8.3).  Nothing else in matching/ counts feasibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Collection, Mapping, Optional, Sequence

from gtgen.contracts import (
    AttributeAlignment,
    CandidateView,
    ComplianceCell,
    DocClaim,
    NormalisedValue,
    ScenarioRole,
)
from gtgen.matching.compliance import (
    DEFAULT_UNVERIFIED_FACTOR,
    compliance_matrix,
    feasible_product_ids,
    hardness_map,
    is_inactive,
)
from gtgen.matching.evidence import DEFAULT_EQ_REL_TOL, ConstraintT
from gtgen.matching.rules import RelaxRule, apply_levels


@dataclass(frozen=True)
class EvalContext:
    """Everything ``compliance_matrix`` needs besides constraints and candidates."""
    alignments: Mapping[str, AttributeAlignment] = field(default_factory=dict)
    normalised: Optional[Mapping[tuple[str, str], NormalisedValue]] = None
    roles: Optional[Mapping[str, ScenarioRole]] = None
    doc_claims: Optional[Mapping[tuple[str, str], DocClaim]] = None
    quantity: Optional[float] = None
    verification_only_fail_blocks: bool = False
    bypassed: Optional[Collection[str]] = None
    unverified_factor: float = DEFAULT_UNVERIFIED_FACTOR
    rel_tol: float = DEFAULT_EQ_REL_TOL

    def cells(self, constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView]) -> list[ComplianceCell]:
        return compliance_matrix(
            constraints, candidates, self.alignments, self.normalised, unverified_factor=self.unverified_factor,
            rel_tol=self.rel_tol, roles=self.roles, doc_claims=self.doc_claims, quantity=self.quantity,
            verification_only_fail_blocks=self.verification_only_fail_blocks, bypassed=self.bypassed)


def feasible_ids(constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView],
                 cells: Sequence[ComplianceCell]) -> list[str]:
    """Role-aware feasible product ids (sorted).  No active constraint -> every candidate is feasible."""
    active = [c for c in constraints if not is_inactive(c)]
    if not active:
        return sorted(c.product_id for c in candidates)
    return feasible_product_ids(cells, hardness_map(active))


@dataclass(frozen=True)
class GainResult:
    before: tuple[str, ...]
    after: tuple[str, ...]
    gained: tuple[str, ...]            # feasible after but not before
    lost: tuple[str, ...]              # feasible before but not after (should be empty for relaxations)
    suppliers_before: int
    suppliers_after: int

    @property
    def gain_products(self) -> int:
        return len(self.after) - len(self.before)

    @property
    def gain_suppliers(self) -> int:
        return self.suppliers_after - self.suppliers_before

    def gain(self, unit: str = "product") -> int:
        return self.gain_suppliers if unit == "supplier" else self.gain_products

    def count_after(self, unit: str = "product") -> int:
        return self.suppliers_after if unit == "supplier" else len(self.after)

    def count_before(self, unit: str = "product") -> int:
        return self.suppliers_before if unit == "supplier" else len(self.before)


def gain_between(before: Sequence[str], after: Sequence[str], candidates: Sequence[CandidateView]) -> GainResult:
    """The counting kernel (the single place gains are computed)."""
    sup = {c.product_id: c.supplier_id for c in candidates}
    b, a = set(before), set(after)
    return GainResult(
        before=tuple(sorted(b)), after=tuple(sorted(a)), gained=tuple(sorted(a - b)), lost=tuple(sorted(b - a)),
        suppliers_before=len({sup.get(p, p) for p in b}), suppliers_after=len({sup.get(p, p) for p in a}))


def feasible_set(constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView], ctx: EvalContext, *,
                 rules: Sequence[RelaxRule] = (), levels: Optional[Mapping[str, int]] = None) -> list[str]:
    """Feasible product ids with ``rules`` relaxed to ``levels`` (ORIGINAL ``constraints`` are never modified)."""
    cons = apply_levels(constraints, rules, levels or {}) if rules and levels else list(constraints)
    return feasible_ids(cons, candidates, ctx.cells(cons, candidates))


def role_aware_gain(constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView], ctx: EvalContext, *,
                    rules: Sequence[RelaxRule] = (), levels_before: Optional[Mapping[str, int]] = None,
                    levels_after: Optional[Mapping[str, int]] = None) -> GainResult:
    """Gain of moving ORIGINAL ``constraints`` from ``levels_before`` (default: all 0) to ``levels_after``.

    Use it for: a menu point (levels of the point), an accepted change (truth ctx -> ``truth_gain``), the
    negotiator's estimate (belief constraints + belief ctx -> ``negotiator_estimated_gain``), a COUNTER (levels with
    the counter level k), and calibration.  ``constraints`` must be the originals; ``levels`` carry the state."""
    return gain_between(feasible_set(constraints, candidates, ctx, rules=rules, levels=levels_before),
                        feasible_set(constraints, candidates, ctx, rules=rules, levels=levels_after), candidates)


def counter_gain(constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView], ctx: EvalContext,
                 rules: Sequence[RelaxRule], ref: str, counter_level: int, *,
                 base_levels: Optional[Mapping[str, int]] = None) -> GainResult:
    """Gain of a COUNTER measured at the COUNTER level (not the proposed one): replaces the legacy
    ``nearest_gain_value`` gain path."""
    base = dict(base_levels or {})
    after = dict(base)
    after[ref] = counter_level
    return role_aware_gain(constraints, candidates, ctx, rules=rules, levels_before=base, levels_after=after)
