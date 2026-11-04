"""D050: belief-side BEST_AVAILABLE ranking and its deterministic wording.

Shared by the GT negotiator and by the loop (when a negotiator under test ignores the wrap-up). Pure functions; the
wording uses only catalogue fields (product / supplier name, formatted price) and the caller's own constraint wording.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence

from gtgen.contracts import (
    BestAvailable, BelievedConstraint, CandidateView, Compliance, ComplianceCell, RankingRow, ScenarioRole,
)
from gtgen.matching.recommend import best_available
from gtgen.ranking.matrix import build_ranking_matrix

from ._util import is_supplier_stat_belief


def cand_label(c: CandidateView) -> str:
    s = c.product_name
    if c.supplier_name:
        s += f" from {c.supplier_name}"
    if c.price_formatted:
        s += f" ({c.price_formatted})"
    return s


def best_available_ranking(raw: Sequence[BelievedConstraint], current: Sequence[BelievedConstraint],
                           candidates: Sequence[CandidateView], raw_cells: Iterable[ComplianceCell],
                           current_cells: Iterable[ComplianceCell], settings: Mapping, *, quantity: float,
                           budget_inr: Optional[float],
                           roles: Optional[Mapping[str, ScenarioRole]] = None) -> list[RankingRow]:
    """The belief-side matrix of the candidates, FULL ranking by score exactly as ``build_ranking_matrix`` produces it
    (rank 1 = highest score, not renumbered). The offer is NOT its first k rows: see ``best_available_view``.

    * ``raw`` / ``raw_cells``: the beliefs BEFORE agreed relaxations are patched in (what the buyer originally asked
      for) and their cells; ``match_original`` and the unmet constraints come from these.
    * ``current`` / ``current_cells``: the beliefs with the agreed relaxations; they give the score columns and decide
      ``non_compliant`` (a FAIL on a negotiated HARD belief).
    """
    raw_cells, current_cells = list(raw_cells), list(current_cells)
    rows = build_ranking_matrix(current, candidates, current_cells, quantity=quantity, budget_inr=budget_inr,
                                weights=settings["ranking"]["default_weights"], buyer_city=None, settings=settings,
                                original_constraints=raw, original_cells=raw_cells, original_roles=roles)
    if any(r.score > 0 for r in rows):          # a meaningful score order exists: keep it (the offer may still differ)
        return rows
    return _offer_sorted(rows, raw, raw_cells, current_cells, settings, roles)


def _offer_sorted(rows: Sequence[RankingRow], raw: Sequence[BelievedConstraint], raw_cells: Sequence[ComplianceCell],
                  current_cells: Sequence[ComplianceCell], settings: Mapping,
                  roles: Optional[Mapping[str, ScenarioRole]]) -> list[RankingRow]:
    """D070 / audit r2: the BEST_AVAILABLE offer order (and, when every score is 0 because every candidate has a HARD
    FAIL / UNKNOWN, the negotiator ranking too: the plain matrix would be product_id order). Order = not non_compliant, match_original desc (the share of the
    negotiated constraints verified), p_feasible desc, then the D070 supplier priority as a strict lexicographic
    order (location_match desc, tenure desc, rating_bayes desc), utility desc, product_id. Ranks are renumbered; the offer is the first k of this order.
    (Default weights rank location > years > rating, D070, so utility ties favour tenure then rating.)"""
    rows = list(rows)
    if not rows:
        return rows
    ba = best_available(rows, raw, raw_cells, k=len(rows), settings=settings, final_cells=current_cells, roles=roles)
    info = {i.product_id: i for i in ba.items}

    def key(r: RankingRow):
        i = info[r.product_id]
        return (i.non_compliant, -round(i.match_original, 12), -round(r.p_feasible, 12),
                -round(r.location_match or 0.0, 12), -(r.tenure_years or 0.0), -round(r.rating_bayes, 12),
                -round(r.utility, 12), r.product_id)

    out = sorted(rows, key=key)
    return [r.model_copy(update={"rank": n}) for n, r in enumerate(out, start=1)]


def best_available_view(ranking: Sequence[RankingRow], raw: Sequence[BelievedConstraint],
                        raw_cells: Iterable[ComplianceCell], current_cells: Iterable[ComplianceCell],
                        settings: Mapping, roles: Optional[Mapping[str, ScenarioRole]] = None) -> BestAvailable:
    """The OFFER: top-k (settings ``recommendation.best_available_k``) of the belief-side ranking by
    matching.recommend.best_available (compliant first, match_original desc, p_feasible desc, product_id), in offer
    order. Independent of the order of ``ranking``."""
    k = int(settings["recommendation"]["best_available_k"])
    raw_cells, current_cells = list(raw_cells), list(current_cells)
    ordered = _offer_sorted(ranking, raw, raw_cells, current_cells, settings, roles)
    ba = best_available(ordered, raw, raw_cells, k=len(ordered), settings=settings, final_cells=current_cells,
                        roles=roles)
    by = {i.product_id: i for i in ba.items}
    return BestAvailable(items=[by[r.product_id] for r in ordered][:k])


def _words(attr: str) -> str:
    return attr.replace("_", " ").strip()


def option_labels(product_id: str, beliefs: Sequence[BelievedConstraint], cells: Iterable[ComplianceCell],
                  exclude_stat: bool = True) -> tuple[list[str], list[str], list[str]]:
    """(met, unmet, to_confirm) plain labels of one product, from the negotiator's own beliefs and cells.

    met = scored PASS / PENDING_VERIFICATION; unmet = a scored, OBSERVED FAIL only; to_confirm = UNKNOWN, no cell, or an
    unscored (verification-only) cell. UNKNOWN is never a shortfall. Priority when one label has several beliefs:
    unmet > to_confirm > met. Supplier rating / years never appear (D070)."""
    by = {c.constraint_ref: c for c in cells if c.product_id == product_id}
    met: list[str] = []
    unmet: list[str] = []
    confirm: list[str] = []
    for b in beliefs:
        lab = _words(b.attribute)
        if not lab or (exclude_stat and is_supplier_stat_belief(b.attribute, b.value, b.unit)):
            continue
        c = by.get(b.belief_id)
        if c is not None and c.scored and c.status == Compliance.FAIL:
            unmet.append(lab)
        elif c is not None and c.scored and c.status in (Compliance.PASS, Compliance.PENDING_VERIFICATION):
            met.append(lab)
        else:
            confirm.append(lab)
    unmet = _unique(unmet)
    confirm = [x for x in _unique(confirm) if x not in unmet]
    met = [x for x in _unique(met) if x not in unmet and x not in confirm]
    return met, unmet, confirm


def _met_labels(product_id: str, beliefs: Sequence[BelievedConstraint], cells: Iterable[ComplianceCell],
                exclude: Sequence[str] = ()) -> list[str]:
    """Plain-language labels of the scored beliefs this product passes (kept for callers that only need ``met``)."""
    return option_labels(product_id, beliefs, cells)[0]


def _unique(xs: Iterable[str]) -> list[str]:
    out: list[str] = []
    for x in xs:
        x = _words(x)
        if x and x not in out:
            out.append(x)
    return out


def final_option(rank: int, c: CandidateView, *, met: Sequence[str], unmet: Sequence[str],
                 to_confirm: Sequence[str]) -> dict:
    """One option of the ``negotiator_final`` facts (negotiator_final v2): separate met / unmet / to_confirm lists."""
    return {"rank": rank, "product": c.product_name, "supplier": c.supplier_name or "",
            "price_text": c.price_formatted or None, "met": _unique(met), "unmet": _unique(unmet),
            "to_confirm": _unique(to_confirm)}


def best_available_facts(view: BestAvailable, ranking: Sequence[RankingRow], candidates: Sequence[CandidateView],
                         beliefs: Sequence[BelievedConstraint], cells: Iterable[ComplianceCell],
                         *, wrapped_up: bool) -> dict:
    """D054 / D070: the FACTS for the LLM-worded offer (plain labels only: no ids, scores, counts, never rating or
    years). Options are in OFFER order (= ``view.items`` order = final.best_available order); ``rank`` is that order.
    Per option: met / unmet (observed FAIL) / to_confirm (UNKNOWN, unmatched, verification-only, pending documents).
    Catalogue fields and the negotiator's own constraint wording only. Wording: ``runners.negotiator_final``."""
    by_id = {c.product_id: c for c in candidates}
    rows = {r.product_id: r for r in ranking}
    cells = list(cells)
    options: list[dict] = []
    confirm: list[str] = []
    for i in view.items:
        if i.product_id not in by_id:
            continue
        r = rows.get(i.product_id)
        met, unmet, unk = option_labels(i.product_id, beliefs, cells)
        extra = list(i.verification_required) + (list(r.pending_verification_docs) if r else [])
        options.append(final_option(len(options) + 1, by_id[i.product_id], met=met, unmet=unmet,
                                    to_confirm=unk + [x for x in extra if _words(x) not in unmet]))
        confirm += options[-1]["to_confirm"]
    if wrapped_up:
        note = "The buyer asked for the best offer available now."
    elif any(not o["unmet"] for o in options):
        note = "Not enough products meet every stated requirement."
    else:
        note = "No product meets every stated requirement and there are no further alternatives to suggest."
    if not options:
        note = "There are no products in the catalogue to offer for this request."
    return {"options": options, "to_confirm": _unique(confirm), "note": note}
