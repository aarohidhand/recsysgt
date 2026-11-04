"""Deterministic compliance checking (PASS / FAIL / UNKNOWN / PENDING_VERIFICATION).

Works on BuyerConstraint (truth) and BelievedConstraint (belief) alike. LLM outputs (attribute
alignment, normalised value, ``is_doc_proof``) are inputs; nothing here calls an LLM.

Rules (CLAUDE.md §1.6/§1.7):
* no aligned evidence, unparsed ``*_raw`` evidence, or unconvertible unit pair -> UNKNOWN (never FAIL);
* evidence contradicts the constraint -> FAIL;
* doc-proof constraint and the candidate claims the attribute (aligned evidence that is not a
  negation, or the document name appears in its texts) -> PENDING_VERIFICATION, ``pending_doc`` set;
  doc-proof without a claim -> UNKNOWN;
* ``feasible``: no HARD FAIL, no HARD UNKNOWN, no SOFT FAIL (PENDING passes, SOFT UNKNOWN does not block).

Scenario extensions (all optional; absent -> behaviour unchanged):
* D048 ``roles`` / ``scenario_role``: cells of a VERIFICATION_ONLY constraint carry ``scored=False`` and are ignored by
  ``feasible`` / ``blockers`` / the relaxation search / the ranking matrix (``verification_only_fail_blocks`` keeps a
  real FAIL scored).
* D051 ``doc_claims``: explicit claim -> PENDING_VERIFICATION; explicit no-claim -> SOFT FAIL("no_claim") or HARD
  UNKNOWN(scored=False); failed/missing claim record -> UNKNOWN as before.  ``bypassed`` = refs whose doc_to_pending
  was accepted: their non-claiming doc-proof cells become PENDING_VERIFICATION.
* D049 synthetic commercial terms are evaluated from catalogue fields (``synthetic_normalised``).
"""
from __future__ import annotations

import re
from typing import Collection, Iterable, Mapping, Optional, Sequence

from gtgen.contracts import (
    AttributeAlignment,
    BelievedConstraint,
    BuyerConstraint,
    CandidateView,
    Compliance,
    ComplianceCell,
    DocClaim,
    FramingDecision,
    Hardness,
    NormalisedValue,
    ScenarioRole,
)
from gtgen.matching.evidence import (
    DEFAULT_EQ_REL_TOL,
    ConstraintT,
    ConstraintView,
    Evidence,
    as_view,
    evaluate,
    extract_evidence,
    hardness_of,
    ref_of,
    less_specific,
    type_mismatch,
)

DEFAULT_UNVERIFIED_FACTOR = 0.9  # ranking.unverified_text_factor


def _conf(ev: Evidence, unverified_factor: float) -> float:
    c = 1.0 if ev.confidence is None else ev.confidence
    return c * unverified_factor if ev.verified is False else c


def check_view(view: ConstraintView, cand: CandidateView, alignment: Optional[AttributeAlignment],
               normalised: Optional[NormalisedValue] = None,
               unverified_factor: float = DEFAULT_UNVERIFIED_FACTOR,
               rel_tol: float = DEFAULT_EQ_REL_TOL) -> tuple[ComplianceCell, Evidence]:
    ev = extract_evidence(view, cand, alignment, normalised)
    if view.is_doc_proof:
        if ev.claim:
            doc = view.doc_name or view.attribute
            cell = ComplianceCell(product_id=cand.product_id, constraint_ref=view.ref,
                                  status=Compliance.PENDING_VERIFICATION, evidence=ev.summary,
                                  evidence_confidence=_conf(ev, unverified_factor), pending_doc=doc)
        else:
            cell = ComplianceCell(product_id=cand.product_id, constraint_ref=view.ref,
                                  status=Compliance.UNKNOWN)
        return cell, ev
    status = evaluate(view, ev, rel_tol)
    conf = None if status == Compliance.UNKNOWN else _conf(ev, unverified_factor)
    mism = status == Compliance.UNKNOWN and type_mismatch(view, ev)
    lessp = status == Compliance.UNKNOWN and not mism and less_specific(view, ev)
    cell = ComplianceCell(product_id=cand.product_id, constraint_ref=view.ref, status=status,
                          evidence="type_mismatch" if mism else ("less_specific" if lessp else ev.summary), evidence_confidence=conf)
    return cell, ev


def check(constraint: ConstraintT, candidate: CandidateView, alignment: Optional[AttributeAlignment],
          normalised: Optional[NormalisedValue] = None, *,
          unverified_factor: float = DEFAULT_UNVERIFIED_FACTOR,
          rel_tol: float = DEFAULT_EQ_REL_TOL) -> ComplianceCell:
    """Compliance of one candidate against one constraint.

    ``evidence_confidence`` on PASS/PENDING cells already includes the ``unverified_factor`` when the
    evidence row has ``raw_text_verified == False`` (so ranking uses it directly as p_c).
    """
    return check_view(as_view(constraint), candidate, alignment, normalised, unverified_factor, rel_tol)[0]


# ------------------------------------------------------------------ D049 synthetic commercial terms
# D070: supplier rating / years are ranking features only.  The evaluators below stay tolerant (a legacy constraint on
# them still evaluates), but they are never counted in match_original (see recommend.negotiated).
RANKING_ONLY_ATTRIBUTES = ("min_supplier_rating", "min_tenure_years")
SYNTHETIC_QUANTITY_TERMS = ("budget_inr", "qty_vs_moq")
_SYNTH_UNIT = {"budget_inr": "INR", "qty_vs_moq": "fraction", "min_supplier_rating": "stars",
               "min_tenure_years": "years", "preferred_states": None}


def is_synthetic(c: ConstraintT) -> bool:
    return isinstance(c, BuyerConstraint) and c.provenance == "synthetic_commercial"


def _synthetic_value(attr: str, cand: CandidateView, quantity: Optional[float]) -> Optional[str]:
    if attr == "budget_inr":
        return None if cand.price is None else repr(float(cand.price) * float(quantity))
    if attr == "qty_vs_moq":
        return None if cand.moq is None else repr(max(0.0, float(cand.moq) / float(quantity) - 1.0))
    if attr == "min_supplier_rating":
        return None if cand.rating is None else repr(float(cand.rating))
    if attr == "min_tenure_years":
        from gtgen.ranking import tenure_years  # lazy: ranking imports matching
        t = tenure_years(cand.member_since)
        return None if t is None else repr(float(t))
    if attr == "preferred_states":
        return cand.state.strip() if cand.state and cand.state.strip() else None
    return None


def synthetic_normalised(constraints: Iterable[ConstraintT], candidates: Sequence[CandidateView],
                         quantity: Optional[float] = None) -> dict[tuple[str, str], NormalisedValue]:
    """(product_id, attribute) -> NormalisedValue built from CATALOGUE FIELDS for every synthetic_commercial
    constraint (llm_call_id "catalogue_field", confidence 1.0).  A missing field gives no entry (-> UNKNOWN).

    budget_inr: price x quantity (INR); qty_vs_moq: max(0, moq/quantity - 1) (fraction); min_supplier_rating:
    rating (stars); min_tenure_years: tenure from member_since (years); preferred_states: state text.
    ``quantity`` is required (ValueError) when a budget_inr / qty_vs_moq term is present.
    """
    synth = [c for c in constraints if is_synthetic(c) and c.framing_decision not in _INACTIVE]
    if not synth:
        return {}
    if any(c.attribute in SYNTHETIC_QUANTITY_TERMS for c in synth) and (quantity is None or quantity <= 0):
        raise ValueError("quantity (> 0) is required to evaluate budget_inr / qty_vs_moq synthetic terms")
    out: dict[tuple[str, str], NormalisedValue] = {}
    for c in synth:
        for cand in candidates:
            val = _synthetic_value(c.attribute, cand, quantity)
            if val is not None:
                out[(cand.product_id, c.attribute)] = NormalisedValue(
                    source_text=f"{c.attribute}={val}", value=val, unit=_SYNTH_UNIT.get(c.attribute),
                    confidence=1.0, llm_call_id="catalogue_field")
    return out


# ------------------------------------------------------------------ roles / inactive constraints
_INACTIVE = (FramingDecision.DROP, FramingDecision.MERGE)


def is_inactive(c: ConstraintT) -> bool:
    """DROPPED / DROP / MERGE constraints must never be scored (defensive: callers should not pass them)."""
    return isinstance(c, BuyerConstraint) and (c.framing_decision in _INACTIVE
                                               or c.scenario_role == ScenarioRole.DROPPED)


def role_of(c: ConstraintT, roles: Optional[Mapping[str, ScenarioRole]] = None) -> ScenarioRole:
    """roles[ref] if given, else BuyerConstraint.scenario_role, else NEGOTIATED."""
    r = roles.get(ref_of(c)) if roles else None
    if r is None and isinstance(c, BuyerConstraint):
        r = c.scenario_role
    return r or ScenarioRole.NEGOTIATED


def _doc_cell(view: ConstraintView, cand: CandidateView, ev: Evidence, dc: Optional[DocClaim],
              unverified_factor: float, bypassed: bool) -> ComplianceCell:
    doc = view.doc_name or view.attribute
    pid, ref = cand.product_id, view.ref
    if dc is None or dc.llm_call_id == "failed":                       # no information (a failed batch is no no-claim)
        status, evid, scored = Compliance.UNKNOWN, None, True
    elif dc.claims:
        return ComplianceCell(product_id=pid, constraint_ref=ref, status=Compliance.PENDING_VERIFICATION,
                              evidence=f'claims: "{dc.quote}"', evidence_confidence=_conf(ev, unverified_factor),
                              pending_doc=doc)
    elif view.hardness == Hardness.HARD:
        status, evid, scored = Compliance.UNKNOWN, "no_claim", False   # per-cell verification_only (D051)
    else:
        status, evid, scored = Compliance.FAIL, "no_claim", True       # SOFT: blocking, doc_to_pending reachable
    if bypassed:
        return ComplianceCell(product_id=pid, constraint_ref=ref, status=Compliance.PENDING_VERIFICATION,
                              evidence="bypass_accepted" + (" (no_claim)" if evid else ""),
                              evidence_confidence=1.0, pending_doc=doc)
    return ComplianceCell(product_id=pid, constraint_ref=ref, status=status, evidence=evid, scored=scored)


def compliance_matrix(constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView],
                      alignments: Mapping[str, AttributeAlignment],
                      normalised: Optional[Mapping[tuple[str, str], NormalisedValue]] = None, *,
                      unverified_factor: float = DEFAULT_UNVERIFIED_FACTOR,
                      rel_tol: float = DEFAULT_EQ_REL_TOL,
                      roles: Optional[Mapping[str, ScenarioRole]] = None,
                      doc_claims: Optional[Mapping[tuple[str, str], DocClaim]] = None,
                      quantity: Optional[float] = None,
                      verification_only_fail_blocks: bool = False,
                      bypassed: Optional[Collection[str]] = None) -> list[ComplianceCell]:
    """Flat list of cells, candidates outer / constraints inner (input order).

    ``alignments`` is keyed by buyer attribute; ``normalised`` by (product_id, buyer attribute).
    New optional arguments: see the module docstring (D048 roles, D051 doc_claims, D049 quantity,
    ``bypassed`` = constraint refs whose doc_to_pending relaxation was accepted).
    DROPPED / DROP / MERGE constraints are skipped (no cell).
    """
    active = [c for c in constraints if not is_inactive(c)]
    synth = synthetic_normalised(active, candidates, quantity) if any(is_synthetic(c) for c in active) else {}
    out: list[ComplianceCell] = []
    for cand in candidates:
        for c in active:
            v = as_view(c)
            if is_synthetic(c):
                cell = check_view(v, cand, None, synth.get((cand.product_id, v.attribute)), unverified_factor,
                                  rel_tol)[0]
            elif doc_claims is not None and v.is_doc_proof:
                nv = normalised.get((cand.product_id, v.attribute)) if normalised else None
                ev = extract_evidence(v, cand, alignments.get(v.attribute), nv)
                cell = _doc_cell(v, cand, ev, doc_claims.get((cand.product_id, v.ref)), unverified_factor,
                                 bool(bypassed) and v.ref in bypassed)
            else:
                nv = normalised.get((cand.product_id, v.attribute)) if normalised else None
                cell = check_view(v, cand, alignments.get(v.attribute), nv, unverified_factor, rel_tol)[0]
                if bypassed and v.ref in bypassed and v.is_doc_proof and cell.status != Compliance.PENDING_VERIFICATION:
                    cell = ComplianceCell(product_id=cand.product_id, constraint_ref=v.ref,
                                          status=Compliance.PENDING_VERIFICATION, evidence="bypass_accepted",
                                          evidence_confidence=1.0, pending_doc=v.doc_name or v.attribute)
            if (roles or isinstance(c, BuyerConstraint)) and role_of(c, roles) == ScenarioRole.VERIFICATION_ONLY:
                if not (verification_only_fail_blocks and cell.scored and cell.status == Compliance.FAIL):
                    cell = cell.model_copy(update={"scored": False})
            out.append(cell)
    return out


def belief_role_reasons(beliefs: Sequence[BelievedConstraint], cells: Iterable[ComplianceCell], n_pool: int,
                        threshold: float, *, demote_zero_pass: bool = True) -> dict[str, tuple[ScenarioRole, str]]:
    """ref -> (role, reason).  reason is "" (negotiated), "zero_pass" or "low_coverage".

    A HARD non-synthetic belief with 0 PASS / PENDING_VERIFICATION cells over the FULL pool is VERIFICATION_ONLY
    ("zero_pass", mirrors the truth-side D069 rule; ``demote_zero_pass``); else one whose non-UNKNOWN share is
    below ``threshold`` is VERIFICATION_ONLY ("low_coverage").  Partial-coverage beliefs (>= 1 PASS) stay HARD.
    """
    known: dict[str, int] = {}
    ok: dict[str, int] = {}
    for cell in cells:
        if cell.status != Compliance.UNKNOWN:
            known[cell.constraint_ref] = known.get(cell.constraint_ref, 0) + 1
        if cell.status in (Compliance.PASS, Compliance.PENDING_VERIFICATION):
            ok[cell.constraint_ref] = ok.get(cell.constraint_ref, 0) + 1
    out: dict[str, tuple[ScenarioRole, str]] = {}
    for b in beliefs:
        ref = ref_of(b)
        if hardness_of(b) != Hardness.HARD:
            out[ref] = (ScenarioRole.NEGOTIATED, "")
            continue
        share = known.get(ref, 0) / n_pool if n_pool > 0 else 0.0
        if demote_zero_pass and not is_synthetic(b) and ok.get(ref, 0) == 0:
            out[ref] = (ScenarioRole.VERIFICATION_ONLY, "zero_pass")
        elif share < threshold:
            out[ref] = (ScenarioRole.VERIFICATION_ONLY, "low_coverage")
        else:
            out[ref] = (ScenarioRole.NEGOTIATED, "")
    return out


def belief_roles(beliefs: Sequence[BelievedConstraint], cells: Iterable[ComplianceCell], n_pool: int,
                 threshold: float, *, demote_zero_pass: bool = True) -> dict[str, ScenarioRole]:
    """Negotiator-side roles from its own cells (catalogue evidence only); see ``belief_role_reasons``.

    SOFT beliefs are NEGOTIATED.  An empty pool gives share 0.  Pass the result as ``roles=`` to a second
    ``compliance_matrix`` call (with ``verification_only_fail_blocks`` observed FAILs keep blocking).
    """
    return {r: v[0] for r, v in belief_role_reasons(beliefs, cells, n_pool, threshold,
                                                    demote_zero_pass=demote_zero_pass).items()}


_LOCATION_ATTR_RE = re.compile(r"\b(state|states|city|cities|location|region|delivery)\b")
_QTY_WORDS = frozenset({"quantity", "qty", "units", "unit", "pieces", "pcs", "nos", "number"})


def _commercial_kind(c: ConstraintT) -> Optional[str]:
    """'budget' | 'moq' for the two synthetic commercial terms, by attribute wording -- the SAME heuristic as
    ``dialogue._commercial.commercial_kind`` (duplicated here to avoid a matching -> dialogue import), since on the
    belief side these are ordinary ``BelievedConstraint`` rows (not ``synthetic_commercial`` ``BuyerConstraint``
    ones), so ``is_synthetic`` alone would miss them."""
    toks = {t for t in re.split(r"[^a-z0-9]+", (c.attribute or "").lower()) if t}
    if "budget" in toks:
        return "budget"
    if "moq" in toks or ("minimum" in toks and toks & _QTY_WORDS and "order" in toks) \
            or ("order" in toks and toks & _QTY_WORDS and (c.operator or "").strip() in ("<=", "<")):
        return "moq"
    return None


def is_spec_constraint(c: ConstraintT) -> bool:
    """A constraint that describes the product itself, not a commercial/ranking term (D075/D081): excludes
    commercial terms (budget, MOQ -- ``is_synthetic`` on the truth side, ``_commercial_kind`` by wording on the
    belief side) and supplier-location terms, which every candidate can satisfy without any product fact being
    confirmed. Works on truth (``BuyerConstraint``) or belief (``BelievedConstraint``) alike."""
    if is_synthetic(c) or _commercial_kind(c) is not None:
        return False
    return not _LOCATION_ATTR_RE.search((c.attribute or "").lower().replace("_", " "))


def verified_feasible_ids(feasible: Sequence[str], constraints: Sequence[ConstraintT],
                          cells: Sequence[ComplianceCell], roles: Optional[Mapping[str, ScenarioRole]]) -> list[str]:
    """D075/D081: the RECOMMEND gate, generalised to truth or belief constraints via ``ref_of``.

    A candidate counts as verified only if, beyond being feasible, at least one scored PASS /
    PENDING_VERIFICATION exists on a NEGOTIATED (non ``VERIFICATION_ONLY``) product-specification constraint.
    Budget / MOQ / supplier-location terms do not count (``is_spec_constraint``). If there is no spec constraint
    at all, every feasible candidate counts (nothing needs verifying); if every spec constraint is
    VERIFICATION_ONLY (or none has a scored PASS), nothing is verified.

    Mirrors ``dialogue._verified.verified_feasible`` exactly (that module now delegates here) so a deterministic
    simulation (calibration, D050) predicts the SAME outcome the live negotiator reaches on this gate, instead of
    assuming RECOMMENDED is reachable whenever the plain (role-aware) feasible count hits the shortlist (D076
    reconciliation; D081)."""
    spec = [c for c in constraints if is_spec_constraint(c)]
    if not spec:
        return list(feasible)
    negotiated = {ref_of(c) for c in spec
                  if not roles or roles.get(ref_of(c), ScenarioRole.NEGOTIATED) != ScenarioRole.VERIFICATION_ONLY}
    good = {z.product_id for z in cells if z.constraint_ref in negotiated and z.scored
            and z.status in (Compliance.PASS, Compliance.PENDING_VERIFICATION)}
    return [p for p in feasible if p in good]


def hardness_map(constraints: Iterable[ConstraintT]) -> dict[str, Hardness]:
    return {ref_of(c): hardness_of(c) for c in constraints}


def feasible(cells: Iterable[ComplianceCell], hardness: Mapping[str, Hardness]) -> bool:
    """Cells of ONE candidate. No HARD FAIL, no HARD UNKNOWN, no SOFT FAIL."""
    for cell in cells:
        if not cell.scored:
            continue
        h = hardness[cell.constraint_ref]
        if h == Hardness.HARD and cell.status in (Compliance.FAIL, Compliance.UNKNOWN):
            return False
        if h == Hardness.SOFT and cell.status == Compliance.FAIL:
            return False
    return True


def cells_by_product(cells: Iterable[ComplianceCell]) -> dict[str, dict[str, ComplianceCell]]:
    out: dict[str, dict[str, ComplianceCell]] = {}
    for c in cells:
        out.setdefault(c.product_id, {})[c.constraint_ref] = c
    return out


def feasible_product_ids(cells: Iterable[ComplianceCell], hardness: Mapping[str, Hardness]) -> list[str]:
    by = cells_by_product(cells)
    return sorted(p for p, d in by.items() if feasible(d.values(), hardness))
