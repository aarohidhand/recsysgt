"""D050 recommendation helpers: match_original, perfect deal, unmet constraints, best-available list.

Pure and deterministic.  A constraint is *negotiated* for these purposes iff it is active (framing_decision
not DROP/MERGE, role not DROPPED) and its role (``roles[ref]``, else ``BuyerConstraint.scenario_role``, else
NEGOTIATED) is NEGOTIATED.  Cells with ``scored=False`` are excluded for that candidate.

Status semantics (one definition for every function here):
* satisfied  = PASS or PENDING_VERIFICATION;
* unmet      = a scored FAIL only.  UNKNOWN / missing cells are NOT unmet: they are "not yet satisfied" and
               belong in ``verification_required`` (dialogue.assemble.reclassify_unknown);
* ``match_original`` = share satisfied, unknown counted as not yet satisfied (UNKNOWN stays in the denominator,
  rule 6: UNKNOWN is never PASS);
* the denominator of ``match_original`` is the weight of the negotiated constraints that have a scored cell
  (or no cell) for that product; unscored cells leave both numerator and denominator.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence

from gtgen.contracts import (
    BestAvailable,
    BestAvailableItem,
    Compliance,
    ComplianceCell,
    Hardness,
    RankingRow,
    ScenarioRole,
)
from gtgen.matching.compliance import RANKING_ONLY_ATTRIBUTES, cells_by_product, is_inactive, role_of
from gtgen.matching.evidence import ConstraintT, hardness_of, ref_of

DEFAULT_MATCH_WEIGHTS = {"hard": 2.0, "soft": 1.0}
_SATISFIED = (Compliance.PASS, Compliance.PENDING_VERIFICATION)


def match_weights(settings: Optional[Mapping]) -> dict[str, float]:
    rec = (settings or {}).get("recommendation", {}) or {}
    mw = rec.get("match_weights", {}) or {}
    return {"hard": float(mw.get("hard", DEFAULT_MATCH_WEIGHTS["hard"])),
            "soft": float(mw.get("soft", DEFAULT_MATCH_WEIGHTS["soft"]))}


def negotiated(constraints: Sequence[ConstraintT],
               roles: Optional[Mapping[str, ScenarioRole]] = None) -> list[ConstraintT]:
    """D070: ranking-only supplier attributes (RANKING_ONLY_ATTRIBUTES) are never negotiated constraints."""
    return [c for c in constraints if not is_inactive(c) and role_of(c, roles) == ScenarioRole.NEGOTIATED
            and getattr(c, "attribute", None) not in RANKING_ONLY_ATTRIBUTES]


def _cell(by: Mapping[str, Mapping[str, ComplianceCell]], pid: str, ref: str) -> Optional[ComplianceCell]:
    return by.get(pid, {}).get(ref)


def match_original(original_constraints: Sequence[ConstraintT], original_cells: Iterable[ComplianceCell],
                   settings: Optional[Mapping] = None, *,
                   roles: Optional[Mapping[str, ScenarioRole]] = None,
                   product_ids: Optional[Iterable[str]] = None) -> dict[str, float]:
    """product_id -> sum(w x satisfied) / sum(w) over the ORIGINAL negotiated constraints.

    w = settings recommendation.match_weights (HARD 2, SOFT 1).  satisfied = PASS or PENDING_VERIFICATION
    (UNKNOWN / FAIL / missing cell = not satisfied; UNKNOWN stays in the denominator).  verification_only constraints and unscored cells are
    excluded.  No counted constraint for a product -> 1.0.  ``roles`` supplies belief-side roles.
    ``product_ids`` adds products that have no cell at all (they get 0.0 if constraints exist, else 1.0).
    """
    w = match_weights(settings)
    cons = negotiated(original_constraints, roles)
    cells = list(original_cells)
    by = cells_by_product(cells)
    pids = sorted(set(by) | set(product_ids or ()))
    out: dict[str, float] = {}
    for pid in pids:
        num = den = 0.0
        for c in cons:
            cell = _cell(by, pid, ref_of(c))
            if cell is not None and not cell.scored:
                continue
            wt = w["hard"] if hardness_of(c) == Hardness.HARD else w["soft"]
            den += wt
            if cell is not None and cell.status in _SATISFIED:
                num += wt
        out[pid] = 1.0 if den == 0 else num / den
    return out


def satisfies_all_negotiated(product_id: str, constraints: Sequence[ConstraintT],
                             cells: Iterable[ComplianceCell], *,
                             roles: Optional[Mapping[str, ScenarioRole]] = None) -> bool:
    """Every negotiated constraint has a scored PASS / PENDING_VERIFICATION cell for the product
    (unscored cells are skipped; a missing cell or UNKNOWN is not satisfied)."""
    by = cells_by_product(cells)
    for c in negotiated(constraints, roles):
        cell = _cell(by, product_id, ref_of(c))
        if cell is not None and not cell.scored:
            continue
        if cell is None or cell.status not in _SATISFIED:
            return False
    return True


def perfect_deal(top1_product_id: Optional[str], final_truth_constraints: Sequence[ConstraintT],
                 final_truth_cells: Iterable[ComplianceCell], *,
                 roles: Optional[Mapping[str, ScenarioRole]] = None) -> bool:
    """D050: the top-1 satisfies every negotiated constraint in the FINAL agreed truth state.
    No top-1 -> False."""
    if top1_product_id is None:
        return False
    return satisfies_all_negotiated(top1_product_id, final_truth_constraints, final_truth_cells, roles=roles)


def unmet_constraints(product_id: str, original_constraints: Sequence[ConstraintT],
                      original_cells: Iterable[ComplianceCell], *,
                      roles: Optional[Mapping[str, ScenarioRole]] = None) -> list[str]:
    """Attributes (constraint order, unique) of negotiated constraints with a scored FAIL cell.
    UNKNOWN / missing cells are not unmet (they go to verification_required)."""
    by = cells_by_product(original_cells)
    out: list[str] = []
    for c in negotiated(original_constraints, roles):
        cell = _cell(by, product_id, ref_of(c))
        if cell is not None and not cell.scored:
            continue
        if cell is not None and cell.status == Compliance.FAIL and c.attribute not in out:
            out.append(c.attribute)
    return out


def unknown_constraints(product_id: str, original_constraints: Sequence[ConstraintT],
                        original_cells: Iterable[ComplianceCell], *,
                        roles: Optional[Mapping[str, ScenarioRole]] = None) -> list[str]:
    """Attributes (constraint order, unique) of negotiated constraints that are neither satisfied nor a scored FAIL:
    UNKNOWN or no cell.  They are "to confirm" and go to ``verification_required``."""
    by = cells_by_product(original_cells)
    out: list[str] = []
    for c in negotiated(original_constraints, roles):
        cell = _cell(by, product_id, ref_of(c))
        if cell is not None and not cell.scored:
            continue
        if (cell is None or cell.status == Compliance.UNKNOWN) and c.attribute not in out:
            out.append(c.attribute)
    return out


def _has_negotiated_hard_fail(product_id: str, constraints: Sequence[ConstraintT], cells: Iterable[ComplianceCell],
                              roles: Optional[Mapping[str, ScenarioRole]]) -> bool:
    # An OBSERVED FAIL on a verification_only HARD constraint is a violation too (CLAUDE.md 1.6): such a cell is only
    # ``scored`` when ``verification_only_fail_blocks`` kept it, so unscored (missing evidence) cells never count.
    by = cells_by_product(cells)
    for c in constraints:
        if is_inactive(c) or hardness_of(c) != Hardness.HARD or role_of(c, roles) == ScenarioRole.DROPPED \
                or getattr(c, "attribute", None) in RANKING_ONLY_ATTRIBUTES:
            continue
        cell = _cell(by, product_id, ref_of(c))
        if cell is not None and cell.scored and cell.status == Compliance.FAIL:
            return True
    return False


def best_available(rows: Sequence[RankingRow], original_constraints: Sequence[ConstraintT],
                   original_cells: Iterable[ComplianceCell], k: Optional[int] = None,
                   settings: Optional[Mapping] = None, *,
                   final_cells: Optional[Iterable[ComplianceCell]] = None,
                   roles: Optional[Mapping[str, ScenarioRole]] = None) -> BestAvailable:
    """Top-k candidates by (compliant first, match_original desc, p_feasible desc, product_id).

    One item per row.  ``non_compliant`` = a scored FAIL on a negotiated HARD constraint, judged on
    ``final_cells`` if given else ``original_cells`` (constraints are the ones passed in; refs must coincide).
    ``k`` defaults to settings recommendation.best_available_k (3).
    """
    if k is None:
        k = int(((settings or {}).get("recommendation", {}) or {}).get("best_available_k", 3))
    ocells = list(original_cells)
    judge = list(final_cells) if final_cells is not None else ocells
    mo = match_original(original_constraints, ocells, settings, roles=roles, product_ids=[r.product_id for r in rows])
    items = []
    for r in rows:
        items.append((_has_negotiated_hard_fail(r.product_id, original_constraints, judge, roles), r))
    keyed = sorted(items, key=lambda t: (t[0], -round(mo[t[1].product_id], 12), -round(t[1].p_feasible, 12),
                                         t[1].product_id))
    return BestAvailable(items=[
        BestAvailableItem(product_id=r.product_id, supplier_id=r.supplier_id, match_original=mo[r.product_id],
                          unmet_constraints=unmet_constraints(r.product_id, original_constraints, ocells, roles=roles),
                          verification_required=list(dict.fromkeys(
                              list(r.verification_required)
                              + unknown_constraints(r.product_id, original_constraints, ocells, roles=roles))),
                          non_compliant=nc)
        for nc, r in keyed[:max(k, 0)]])
