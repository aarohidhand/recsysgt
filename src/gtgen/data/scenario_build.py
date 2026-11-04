"""Pre-dialogue scenario construction for ONE (bid, persona seed) (D046, D048, D049, D051).

OWNER: data-steward. Pure orchestration of existing functions; the only randomness is the seeds.

    prep (reframe -> doc_classify -> align, cache-backed)
      -> D046 merge -> document claims (D051) -> cells -> coverage -> roles (D048)
      -> commercial bands -> persona (picks inside the bands) -> deterministic tightening (D049)
      -> final ScenarioBrief + Persona.

``build_scenario`` never invents a persona: if the persona call fails the result has ``ok=False``.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from gtgen.contracts import (
    BuyerConstraint,
    BuyerProfile,
    Compliance,
    ComplianceCell,
    DocClaim,
    FramingDecision,
    Hardness,
    Persona,
    ScenarioBrief,
    ScenarioRole,
)
from gtgen.data.claim_cache import ClaimCache, claim_key
from gtgen.data.calibrate import (
    invalid_scenario_skip, loosest_feasible, scenario_validity, unsatisfiable_blockers,
)
from gtgen.data.scenario import (
    assign_roles,
    build_commercial_constraints,
    commercial_bands,
    compute_coverage,
    demote_zero_pass,
    find_conflicts,
    reconcile_attributes,
    coverage_meta,
    field_coverage,
    load_cached_normalised,
    tighten,
    tighten_trajectory,
    tightness_floors,
    tightness_target,
)

logger = logging.getLogger(__name__)
_INACTIVE = (FramingDecision.DROP, FramingDecision.MERGE)
PREFILTER_ID = "prefilter:no_term_overlap"
FAILED_ID = "failed"


# ------------------------------------------------------------------ document claims (D051)
def _doc_constraints(constraints: Sequence[BuyerConstraint]) -> list[BuyerConstraint]:
    return [c for c in constraints
            if c.is_doc_proof and c.framing_decision not in _INACTIVE and c.provenance != "synthetic_commercial"]


def _all_claim_pairs(constraints: Sequence[BuyerConstraint], catalogue, settings: Mapping) -> list[tuple]:
    """[(product_id, constraint_id, doc_name, listing_text, passes_prefilter)] for every (candidate, doc-proof ACTIVE
    constraint). ``listing_text`` is the text actually sent (truncated to ``doc_claims.max_listing_chars``)."""
    from gtgen.llm.claims import listing_text, maybe_claims
    max_chars = int(settings["doc_claims"]["max_listing_chars"])
    docs = _doc_constraints(constraints)
    rows: list[tuple] = []
    for cand in catalogue.candidates:
        text = listing_text(cand, max_chars)
        for c in docs:
            name = c.doc_name or c.attribute
            rows.append((cand.product_id, c.constraint_id, name, text, bool(maybe_claims(text, name, dict(settings)))))
    return rows


def claim_requests(constraints: Sequence[BuyerConstraint], catalogue, settings: Mapping
                   ) -> tuple[list[tuple[str, str]], list]:
    """(all (product_id, constraint_id) pairs of the doc-proof ACTIVE constraints,
    DocClaimRequests that pass the prefilter). Pure; no LLM."""
    from gtgen.llm import DocClaimRequest
    rows = _all_claim_pairs(constraints, catalogue, settings)
    return ([(r[0], r[1]) for r in rows],
            [DocClaimRequest(r[0], r[1], r[2], r[3]) for r in rows if r[4]])


def claim_chunks(reqs: Sequence, runners, settings: Mapping) -> list[list]:
    """The request chunks ``fetch_doc_claims`` sends, one per LLM call (D062).

    Token-aware: exactly the batches ``runners.plan_doc_claims`` plans (the same ones ``runners.doc_claims`` re-derives
    for a chunk, so executed == planned). A runner without ``plan_doc_claims`` (test double) falls back to the fixed
    ``doc_claims.batch_size``.
    """
    plan_fn = getattr(runners, "plan_doc_claims", None)
    if callable(plan_fn):
        return [[reqs[i] for i in pb.indices] for pb in plan_fn(list(reqs))]
    n = int(settings["doc_claims"]["batch_size"])
    return [list(reqs[i:i + n]) for i in range(0, len(reqs), n)]


class ClaimMap(dict):
    """``(product_id, constraint_id) -> DocClaim`` (a plain dict for every consumer) that also carries the populated
    ``claim_cache`` (D071) and the dedup ``stats`` of the fetch that produced it."""
    claim_cache: Optional[ClaimCache] = None
    stats: Optional[dict] = None


def fetch_doc_claims(constraints: Sequence[BuyerConstraint], catalogue, runners, settings: Mapping, *,
                     gate=None, scenario_id: str = "doc_claims", persona_seed: int = 0,
                     cache: Optional[ClaimCache] = None) -> "ClaimMap":
    """(product_id, constraint_id) -> DocClaim for every (candidate, doc-proof ACTIVE constraint).

    Requests that fail the prefilter get an explicit no-claim (llm_call_id ``prefilter:no_term_overlap``, no LLM
    call). D071: the remaining pairs are grouped by ``claim_key`` (normalised standard + hash of the exact listing text);
    ONE representative per key not already in ``cache`` goes to ``runners.doc_claims`` (rule-resolved ones inside the
    runner cost no call) in the token-aware batches of ``runners.plan_doc_claims`` (D062), and the result is broadcast to
    every pair sharing the key (per-pair product_id/constraint_ref; the quote is re-verified against the pair's own
    listing). A failed batch yields no-claim records with llm_call_id ``failed`` (= no information -> UNKNOWN
    downstream; failures are not cached). ``gate`` is accepted for compatibility and unused (D066: no request cap).
    Every batch carries ``ConversationContext(scenario_id, persona_seed)`` (D054: required in LIVE mode; claims are
    per bid and shared by its personas, so callers pass the bid-level ``scenario_id`` and the default seed 0).
    The result is a ``ClaimMap`` (dict) with ``.claim_cache`` and ``.stats`` attached.
    """
    from gtgen.llm import DocClaimRequest
    from gtgen.llm.context import ConversationContext
    ctx = ConversationContext(scenario_id=scenario_id, cache_seed=persona_seed)
    cache = cache if cache is not None else ClaimCache()
    rows = _all_claim_pairs(constraints, catalogue, settings)
    out = ClaimMap()
    reqs = []
    for pid, cid, name, text, passes in rows:
        if passes:
            reqs.append(DocClaimRequest(pid, cid, name, text))
            continue
        out[(pid, cid)] = DocClaim(product_id=pid, constraint_ref=cid, claims=False, quote=None,
                                   quote_verified=False, llm_call_id=PREFILTER_ID)
        cache.put(claim_key(text, name), claims=False, quote=None, quote_verified=False, path="prefilter",
                  llm_call_id=PREFILTER_ID)
    keys = [claim_key(r.listing_text, r.doc_name) for r in reqs]
    reps: dict[str, Any] = {}
    n_hits = 0
    for k, r in zip(keys, reqs):
        if k in cache and cache._e[k]["path"] != "prefilter":
            n_hits += 1
        elif k not in reps:
            reps[k] = r
    unique = len(set(keys))
    rep_list = list(reps.values())
    chunks = claim_chunks(rep_list, runners, settings) if rep_list else []
    in_chunk = {id(r) for ch in chunks for r in ch}
    leftover = [r for r in rep_list if id(r) not in in_chunk]   # rule-resolved: runner makes no LLM call for these
    failed_keys: set[str] = set()
    for chunk in chunks + ([leftover] if leftover else []):
        res = runners.doc_claims(chunk, ctx=ctx)
        vals = list(res.value or [])
        ok = len(vals) == len(chunk) and all(
            v.product_id == r.product_id and v.constraint_ref == r.constraint_ref for v, r in zip(vals, chunk))
        for k, r in enumerate(chunk):
            d = vals[k] if ok else None
            if d is None or d.llm_call_id == FAILED_ID or not cache.put_claim(r, d):
                failed_keys.add(claim_key(r.listing_text, r.doc_name))
    n_rule = sum(1 for k in set(keys) if k in cache and k in reps and cache._e[k]["path"] == "rule")
    for k, r in zip(keys, reqs):
        if k in failed_keys:
            out[(r.product_id, r.constraint_ref)] = DocClaim(
                product_id=r.product_id, constraint_ref=r.constraint_ref, claims=False, quote=None,
                quote_verified=False, llm_call_id=FAILED_ID)
        else:
            out[(r.product_id, r.constraint_ref)] = cache.broadcast([r])[0]
    n_pairs, n_pre = len(rows), len(rows) - len(reqs)
    n_calls = len(chunks)
    stats = {"pairs": n_pairs, "unique": unique, "deduped": len(reqs) - unique, "cache_hits": n_hits,
             "prefiltered": n_pre, "rule": n_rule, "llm_items": sum(len(c) for c in chunks), "llm_calls": n_calls,
             "failed": len(failed_keys)}
    cache.dedup += stats["deduped"]
    logger.info("doc_claims %s: n_pairs=%d n_unique_keys=%d n_deduped=%d n_cache_hits=%d n_prefilter=%d n_rule=%d "
                "n_llm_calls=%d (llm_items=%d)", scenario_id, n_pairs, unique, stats["deduped"], n_hits, n_pre,
                n_rule, n_calls, stats["llm_items"])
    out.claim_cache, out.stats = cache, stats
    return out


def claim_stats(doc_claims: Optional[Mapping[tuple[str, str], DocClaim]]) -> dict:
    d = list((doc_claims or {}).values())
    pre = sum(1 for x in d if x.llm_call_id.startswith("prefilter:"))
    return {"n_requests": len(d) - pre, "n_claims": sum(1 for x in d if x.claims), "n_prefiltered": pre,
            "n_failed": sum(1 for x in d if x.llm_call_id == FAILED_ID)}


def merged_constraints(prep, settings: Mapping) -> list[BuyerConstraint]:
    """D046 (narrowed) merge applied to the reframed + classified constraints of a prepared bid."""
    from gtgen.matching import merge_clause_conformity
    return merge_clause_conformity(prep.brief.constraints, settings)


# ------------------------------------------------------------------ result
@dataclass(frozen=True)
class BuiltScenario:
    bid_id: str
    persona_seed: int
    ok: bool
    qa_flags: tuple[str, ...]
    prep: Any = None
    brief: Optional[ScenarioBrief] = None
    persona: Optional[Persona] = None
    profile: Optional[BuyerProfile] = None
    cells: tuple[ComplianceCell, ...] = ()
    doc_claims: Mapping = field(default_factory=dict)
    claim_cache: Optional[ClaimCache] = None     # D071: populated by the table-stage fetch (None if doc_claims were passed in without one)
    bands: Mapping = field(default_factory=dict)
    tightness: Mapping = field(default_factory=dict)
    claims: Mapping = field(default_factory=dict)
    initial_verified_feasible: int = 0           # after tightening (the value the dialogue starts from)
    initial_verified_feasible_before: int = 0    # LLM picks, before tightening
    soft_only_blocked: int = 0
    n_negotiated_hard: int = 0
    n_negotiated_soft: int = 0                   # includes the synthetic commercial terms
    n_synthetic: int = 0
    n_verification_only: int = 0                 # both hardnesses (= hard + soft)
    n_verification_only_hard: int = 0
    n_verification_only_soft: int = 0
    n_dropped: int = 0
    n_merged: int = 0                            # merged groups (constraints carrying merged_from)
    calibration: Mapping = field(default_factory=dict)   # D050 calibration record (empty when skipped)
    blockers: Mapping = field(default_factory=dict)      # soft-only blockers split by what blocks them
    min_shortlist: int = 0
    persona_regenerations: int = 0               # D060 company-uniqueness regenerations used (0 on the legacy path)
    normalised: Mapping = field(default_factory=dict)   # the build's normalised evidence (pass to run_dialogue(normalised=...))
    valid: bool = True                           # D073 validity gate (scenario_meta['validity']); False -> qa_flags 'invalid_scenario:<reason>'
    invalid_reasons: tuple[str, ...] = ()

    def skip_reason(self, settings: Mapping) -> Optional[str]:
        """'invalid_scenario:<reasons>' when settings scenario.require_valid_for_negotiation is true and this scenario is invalid."""
        return invalid_scenario_skip(self.valid, self.invalid_reasons, settings)

    @property
    def negotiation_triggers_naturally(self) -> bool:
        return self.ok and self.initial_verified_feasible < self.min_shortlist and self.soft_only_blocked >= 1


def scenario_summary(b: BuiltScenario) -> dict:
    """JSON-serialisable summary numbers of one built scenario (no secrets, no utterances)."""
    p = b.persona
    return {
        "bid": b.bid_id, "persona_seed": b.persona_seed, "ok": b.ok, "qa_flags": list(b.qa_flags),
        "tau": p.tau if p else None, "tier": p.tier.value if p else None,
        "picks": dict(p.commercial_terms) if p else {},
        "bands_terms": sorted(dict(b.bands.get("terms", {}))), "omitted_terms": dict(b.bands.get("omitted", {})),
        "tightness": dict(b.tightness), "claims": dict(b.claims), "min_shortlist": b.min_shortlist,
        "counts": {"negotiated_hard": b.n_negotiated_hard, "negotiated_soft": b.n_negotiated_soft,
                   "synthetic": b.n_synthetic, "verification_only": b.n_verification_only,
                   "verification_only_hard": b.n_verification_only_hard,
                   "verification_only_soft": b.n_verification_only_soft,
                   "dropped": b.n_dropped, "merged_groups": b.n_merged},
        "initial_verified_feasible_before": b.initial_verified_feasible_before,
        "initial_verified_feasible_after": b.initial_verified_feasible,
        "soft_only_blocked": b.soft_only_blocked,
        "blockers": dict(b.blockers),
        "calibration": {k: v for k, v in dict(b.calibration).items() if k != "candidates"} if b.calibration else {},
        "negotiation_triggers_naturally": b.negotiation_triggers_naturally,
        "n_pool": len(b.prep.catalogue.candidates) if b.prep is not None else None,
        "role_counts": dict(b.brief.scenario_meta.get("role_counts", {})) if b.brief else {},
        "persona_regenerations": b.persona_regenerations,
        "valid": b.valid, "invalid_reasons": list(b.invalid_reasons),
    }


# ------------------------------------------------------------------ build
def _soft_only_blocked(cells: Sequence[ComplianceCell], hmap: Mapping[str, Hardness]) -> int:
    by: dict[str, list[ComplianceCell]] = {}
    for c in cells:
        if c.scored:
            by.setdefault(c.product_id, []).append(c)
    n = 0
    for cs in by.values():
        hard_bad = any(hmap[c.constraint_ref] == Hardness.HARD and c.status in (Compliance.FAIL, Compliance.UNKNOWN)
                       for c in cs)
        soft_fail = any(hmap[c.constraint_ref] == Hardness.SOFT and c.status == Compliance.FAIL for c in cs)
        n += int((not hard_bad) and soft_fail)
    return n


BLOCKER_KEYS = ("real_soft_blocked", "synthetic_commercial_blocked", "observed_fail_blocked", "mixed_blocked")


def soft_blocker_split(cells: Sequence[ComplianceCell], constraints: Sequence[BuyerConstraint]) -> dict:
    """Split the soft-only blockers (candidates with no scored HARD FAIL/UNKNOWN and >= 1 scored SOFT FAIL) by WHAT
    fails: ``real`` = a negotiated SOFT constraint derived from the bid, ``synthetic`` = a synthetic_commercial term,
    ``observed`` = an OBSERVED FAIL on a verification_only SOFT constraint (scored only when
    ``verification_only_fail_blocks``).  A candidate failing exactly one kind lands in that kind's bucket, one failing
    several kinds in ``mixed_blocked`` (so the four buckets sum to ``soft_only_blocked``).  ``involves_*`` count a
    candidate once per kind it fails (not exclusive).  Pure; no tuning."""
    kind: dict[str, str] = {}
    hard: dict[str, bool] = {}
    for c in constraints:
        if c.framing_decision in _INACTIVE or c.scenario_role == ScenarioRole.DROPPED:
            continue
        hard[c.constraint_id] = c.hardness == Hardness.HARD
        if c.provenance == "synthetic_commercial":
            kind[c.constraint_id] = "synthetic"
        elif c.scenario_role == ScenarioRole.VERIFICATION_ONLY:
            kind[c.constraint_id] = "observed"
        else:
            kind[c.constraint_id] = "real"
    by: dict[str, list[ComplianceCell]] = {}
    for c in cells:
        if c.scored and c.constraint_ref in hard:
            by.setdefault(c.product_id, []).append(c)
    out = {k: 0 for k in BLOCKER_KEYS}
    out.update({"involves_real": 0, "involves_synthetic": 0, "involves_observed": 0})
    for cs in by.values():
        if any(hard[c.constraint_ref] and c.status in (Compliance.FAIL, Compliance.UNKNOWN) for c in cs):
            continue
        kinds = {kind[c.constraint_ref] for c in cs if (not hard[c.constraint_ref]) and c.status == Compliance.FAIL}
        if not kinds:
            continue
        for k in kinds:
            out[f"involves_{k}"] += 1
        if len(kinds) > 1:
            out["mixed_blocked"] += 1
        else:
            out[{"real": "real_soft_blocked", "synthetic": "synthetic_commercial_blocked",
                 "observed": "observed_fail_blocked"}[next(iter(kinds))]] += 1
    return out


def build_scenario(bid_id: str, settings: Mapping, runners, *, persona_seed: int,
                   doc_claims: Optional[Mapping[tuple[str, str], DocClaim]] = None,
                   normalised: Optional[Mapping] = None, prepared=None, calibrate: bool = True,
                   run_key: Optional[str] = None, n_scenarios: Optional[int] = None,
                   scenario_index: Optional[int] = None,
                   accepted_profiles: Optional[list] = None) -> BuiltScenario:
    """Pre-dialogue construction for one (bid, persona seed). ``prepared`` (optional) reuses a PreparedBid
    already obtained from ``gtgen.cli.prepare_bid`` (the cache-backed reframe/doc_classify/align step).

    Every LLM call carries ``ConversationContext(scenario_id, persona_seed)`` (D054, required live). ``calibrate``
    (default True) runs the deterministic D050 calibration over the tightness candidates when
    ``recommendation.tier_targets`` exists in ``settings`` (otherwise it is skipped and the plain D049 tightening
    result is used).

    D060 run context (all optional; without ``run_key`` the legacy single-call persona path is used unchanged):
    ``run_key`` + ``n_scenarios`` (+ ``scenario_index``, the global index of this scenario in the run) make the
    persona carry Latin-hypercube strata (``persona_strata``) and go through ``persona_unique`` (company-uniqueness
    regeneration; each regeneration is one more persona call). ``accepted_profiles`` is the caller's list of
    BuyerProfile dicts of earlier scenarios in the run; the profile of this scenario is APPENDED to it (mutated in
    place). A company still not unique after ``max_company_regenerations`` is kept with the qa_flag
    ``company_not_unique:...`` in ``BuiltScenario.qa_flags``."""
    from gtgen.llm.context import ConversationContext
    from gtgen.matching import compliance_matrix, feasible_product_ids, hardness_map

    if prepared is None:
        from gtgen.cli import prepare_bid
        prepared = prepare_bid(bid_id, settings, runners)
    prep = prepared
    brief0 = prep.brief
    catalogue = prep.catalogue
    cands = list(catalogue.candidates)
    qa: list[str] = list(prep.qa_flags)
    sc = settings["scenario"]
    min_shortlist = int(settings["negotiation"]["min_shortlist"])
    quantity = float(brief0.quantity)

    constraints = merged_constraints(prep, settings)
    active = [c for c in constraints if c.framing_decision not in _INACTIVE]
    # ONE alignment selection for build time and the dialogue's truth state: duplicates of a buyer_attribute are
    # resolved by dialogue._util.alignment_dict (highest confidence, first on ties).  A last-wins dict here made the
    # build see number_of_cores -> 'number_of_cores' (37 PASS) while run_dialogue saw 'number_of_core' (0 PASS).
    from gtgen.dialogue._util import alignment_dict
    al = alignment_dict(prep.alignments, strip_bid=False, pool=cands)

    scenario_id = f"{prep.scenario_id_base}_s{persona_seed}"
    ctx = ConversationContext(scenario_id=scenario_id, cache_seed=persona_seed)
    if doc_claims is None:
        doc_claims = fetch_doc_claims(constraints, catalogue, runners, settings, gate=None,
                                      scenario_id=scenario_id, persona_seed=persona_seed)
    cstats = claim_stats(doc_claims)
    if cstats["n_failed"]:
        qa.append(f"doc_claims_failed:{cstats['n_failed']}")
    if normalised is None:
        normalised = load_cached_normalised(bid_id, catalogue)

    kw = dict(unverified_factor=settings["ranking"]["unverified_text_factor"],
              rel_tol=settings["negotiation"]["bundle_search"]["numeric_eq_rel_tolerance"],
              quantity=quantity, verification_only_fail_blocks=bool(sc["verification_only_fail_blocks"]))

    # coverage -> roles (GEM constraints only)
    coverage = compute_coverage(constraints, cands, al, normalised=normalised, doc_claims=doc_claims,
                                settings=settings)
    # attribute reconciliation: one constraint per normalised attribute BEFORE roles (alternates / conflicts -> DROP)
    constraints, attr_records, attr_flags = reconcile_attributes(constraints, coverage)
    qa += attr_flags
    roled = assign_roles(constraints, coverage, settings)
    roled_active = [c for c in roled if c.framing_decision not in _INACTIVE]
    roles = {c.constraint_id: c.scenario_role for c in roled_active if c.scenario_role is not None}
    # D069 zero-PASS demotion: truth-side cells of the final (non-synthetic) constraint set over the FULL pool
    zp_meta: dict = {}
    if bool(sc.get("demote_zero_pass", True)):
        zcells = compliance_matrix(roled_active, cands, al, normalised, roles=roles, doc_claims=doc_claims, **kw)
        npass: dict[str, int] = {}
        for z in zcells:
            if z.status in (Compliance.PASS, Compliance.PENDING_VERIFICATION):
                npass[z.constraint_ref] = npass.get(z.constraint_ref, 0) + 1
        roled, zp_meta = demote_zero_pass(roled, npass, settings)
        roled_active = [c for c in roled if c.framing_decision not in _INACTIVE]
        roles = {c.constraint_id: c.scenario_role for c in roled_active if c.scenario_role is not None}
    else:
        zp_meta = {"enabled": False, "demoted_zero_pass": [], "pass_counts_demoted": {}}
    n_norm = len({pid for pid, _ in normalised})
    cmeta = coverage_meta(coverage, roled, len(cands), n_norm, settings)
    _rr = Counter(c.role_reason for c in roled if c.scenario_role == ScenarioRole.VERIFICATION_ONLY)
    zp_meta["role_reason_counts"] = {"low_coverage": int(_rr.get("low_coverage", 0)),
                                     "zero_pass": int(_rr.get("zero_pass", 0))}
    cmeta["zero_pass"] = zp_meta
    cmeta["attribute_reconciliation"] = attr_records
    leftover = find_conflicts(roled)           # validator: must be empty after reconcile_attributes
    for k in leftover:
        qa.append(f"conflicting_constraints:{k}")
    cmeta["unresolved_conflicts"] = leftover

    # commercial terms
    bands = commercial_bands(catalogue, quantity, settings)
    target = tightness_target(persona_seed, settings)
    fcov = field_coverage(cands, settings)
    cats = [c.constraint_category for c in roled_active] + \
           [sc["commercial_terms"]["category"][t] for t in bands["terms"]]
    regenerations = 0
    if run_key is not None:
        if n_scenarios is None:
            raise ValueError("build_scenario: run_key needs n_scenarios (D060)")
        from gtgen.llm.strata import persona_strata, persona_unique, personas_for_category, tau_band_for_bid
        strata = persona_strata(persona_seed, int(n_scenarios), run_key, settings, cats, index=scenario_index)
        if "tau" in strata:
            # D080 (user decision, 2026-11-01): tau is derived from the BID, not from this scenario's run-wide
            # diversity slot. A category may split the range across its personas; persona_seed is 1-based, so the
            # persona's own index inside the bid is persona_seed - 1.
            n_p = personas_for_category(prep.product_category)
            strata = {**strata, "tau": {**strata["tau"],
                                        "band": tau_band_for_bid(bid_id, run_key, settings,
                                                                 persona_idx=persona_seed - 1, n_personas=n_p)}}
        accepted = accepted_profiles if accepted_profiles is not None else []
        res = persona_unique(
            runners, accepted, n=int(n_scenarios), product_category=prep.product_category, quantity=quantity,
            quantity_unit=brief0.quantity_unit, budget_inr=None, categories=cats, scenario_seed=persona_seed,
            scenario_id=f"{bid_id}:s{persona_seed}", bands=bands, tightness_target=target, ctx=ctx, strata=strata)
        regenerations = int((res.extra or {}).get("regenerations", 0))
        if res.value is not None:
            pr = res.value[0]
            accepted.append({"company_name": pr.company_name, "industry": pr.industry, "city": pr.city})
    else:
        res = runners.persona(
            prep.product_category, quantity, brief0.quantity_unit, None, cats,
            persona_seed, f"{bid_id}:s{persona_seed}", bands=bands, tightness_target=target, ctx=ctx)
    if res.value is None:
        qa += list(res.qa_flags) or ["persona_failed"]
        return BuiltScenario(bid_id, persona_seed, False, tuple(qa), prep=prep, doc_claims=doc_claims,
                             claim_cache=getattr(doc_claims, "claim_cache", None),
                             bands=bands, claims=cstats, min_shortlist=min_shortlist)
    qa += list(res.qa_flags)
    profile, persona = res.value
    sales_persona = (res.extra or {}).get("sales_persona")     # D054: only reaches the negotiator via run_dialogue
    # audit defect 5b: a persona that may accept no relaxation at all makes every negotiation impossible
    scenario_flags: dict = {}
    if persona.max_relaxations is not None and int(persona.max_relaxations) < 1:
        scenario_flags["max_relaxations_raised"] = {"from": int(persona.max_relaxations), "to": 1}
        persona = persona.model_copy(update={"max_relaxations": 1})
    picks = dict(persona.commercial_terms)

    def cells_for(p: Mapping) -> tuple[list[ComplianceCell], list[BuyerConstraint]]:
        synth = build_commercial_constraints(bid_id, quantity, p, settings, field_coverage=fcov)
        allc = roled_active + synth
        return compliance_matrix(allc, cands, al, normalised, roles=roles, doc_claims=doc_claims, **kw), allc

    def count_fn(p: dict) -> int:
        cells, allc = cells_for(p)
        return len(feasible_product_ids(cells, hardness_map(allc)))

    max_steps = int(sc["commercial_terms"]["max_tightening_steps"])
    floors = tightness_floors(catalogue, quantity, settings)       # D073: snap below the band when the walk cannot reach the target
    picks2, tmeta = tighten(picks, bands, count_fn, target, min_shortlist, max_steps, floors=floors)
    cells, allc = cells_for(picks2)
    hmap = hardness_map(allc)
    after = len(feasible_product_ids(cells, hmap))

    # D050 calibration (deterministic, no LLM): simulate the negotiation for each tightness candidate
    calibration: dict = {}
    band = None
    if calibrate:
        from gtgen.data.calibrate import tier_band
        band = tier_band(settings, persona.tier)
    if band is not None:
        picks2, tmeta, cells, allc, hmap, after, calibration, cal_flags = _calibrate(
            settings, persona, profile, brief0, cands, al, normalised, roles, doc_claims, kw, quantity,
            min_shortlist, scenario_id, persona_seed, picks, bands, count_fn, max_steps, target, band,
            (picks2, tmeta, cells, allc, hmap, after), cells_for, roled_active, floors)
        qa += cal_flags
    if after >= min_shortlist:
        qa.append("tightness_not_reached")
    # audit defect 5a: a negotiation path must exist (initial feasible + reachable by relaxing SOFT terms >= shortlist)
    loosest = loosest_feasible(cells, hmap)
    if loosest < min_shortlist:
        flag_u = str((settings.get("recommendation") or {}).get("uncalibrated_flag", "uncalibrated"))
        for f in ("unsatisfiable", flag_u):
            if f not in qa:
                qa.append(f)
    scenario_flags.update({"initial_feasible": after, "loosest_feasible": loosest,
                           "calibration_assumes_perfect_beliefs": True})
    synth2 = build_commercial_constraints(bid_id, quantity, picks2, settings, field_coverage=fcov)
    # D073 validity gate: a negotiated product constraint, a reachable shortlist and a SOFT relaxation path
    n_neg_product = sum(1 for c in allc if c.provenance != "synthetic_commercial"
                        and c.scenario_role == ScenarioRole.NEGOTIATED and c.framing_decision not in _INACTIVE)
    validity = scenario_validity(n_neg_product, after, loosest, _soft_only_blocked(cells, hmap), min_shortlist)
    for r in validity["reasons"]:
        qa.append(f"invalid_scenario:{r}")
    # audit: PASS count of every negotiated non-synthetic HARD constraint (truth side, final cells) and, when the
    # scenario is jointly infeasible, the single HARD constraints whose removal alone would restore a shortlist
    hard_neg = [c.constraint_id for c in allc if c.provenance != "synthetic_commercial" and c.hardness == Hardness.HARD
                and (c.scenario_role == ScenarioRole.NEGOTIATED or c.role_reason == "zero_pass")]
    hard_pass_counts = {cid: 0 for cid in hard_neg}
    for z in cells:
        if z.constraint_ref in hard_pass_counts and z.status in (Compliance.PASS, Compliance.PENDING_VERIFICATION):
            hard_pass_counts[z.constraint_ref] += 1
    blockers_hard = unsatisfiable_blockers(cells, hmap, min_shortlist) if loosest < min_shortlist else []

    final_constraints = list(roled) + synth2
    consistency = _build_truth_consistency(final_constraints, cands, prep.alignments, normalised, doc_claims, kw,
                                           quantity, cells)
    merged = [c for c in roled if c.merged_from]
    meta = dict(cmeta)
    meta.update({
        "bands": bands, "omitted_terms": dict(bands.get("omitted", {})), "tightness": dict(tmeta),
        "initial_feasible": after, "loosest_feasible": loosest,
        "hard_pass_counts": hard_pass_counts, "unsatisfiable_blockers": blockers_hard,
        "calibration_assumes_perfect_beliefs": True,
        **consistency,
        "max_relaxations_raised": scenario_flags.get("max_relaxations_raised"),
        "claims": cstats,
        "doc_claim_stats": dict(getattr(doc_claims, "stats", None) or {}),
        "calibration": calibration, "validity": validity,
        "sales_persona": dict(sales_persona) if sales_persona else None,
        "merged_groups": [{"merged_id": c.constraint_id, "standard": c.buyer_value, "hardness": c.hardness.value,
                           "members": list(c.merged_from)} for c in merged],
    })
    budget = float(picks2["budget_inr"]) if "budget_inr" in picks2 else brief0.budget_inr
    brief = brief0.model_copy(update={
        "scenario_id": f"{prep.scenario_id_base}_s{persona_seed}", "constraints": final_constraints,
        "budget_inr": budget, "scenario_meta": meta})
    persona2 = persona.model_copy(update={"commercial_terms": picks2})

    neg = [c for c in final_constraints if c.scenario_role == ScenarioRole.NEGOTIATED]
    vo = [c for c in final_constraints if c.scenario_role == ScenarioRole.VERIFICATION_ONLY]
    return BuiltScenario(
        bid_id, persona_seed, True, tuple(qa), prep=prep, brief=brief, persona=persona2, profile=profile,
        cells=tuple(cells), doc_claims=doc_claims, claim_cache=getattr(doc_claims, "claim_cache", None), bands=bands, tightness=tmeta, claims=cstats,
        initial_verified_feasible=after, initial_verified_feasible_before=int(tmeta["count_before"]),
        soft_only_blocked=_soft_only_blocked(cells, hmap), calibration=calibration,
        blockers=soft_blocker_split(cells, allc),
        n_negotiated_hard=sum(1 for c in neg if c.hardness == Hardness.HARD),
        n_negotiated_soft=sum(1 for c in neg if c.hardness == Hardness.SOFT),
        n_synthetic=len(synth2),
        n_verification_only=len(vo), n_verification_only_hard=sum(1 for c in vo if c.hardness == Hardness.HARD),
        n_verification_only_soft=sum(1 for c in vo if c.hardness == Hardness.SOFT),
        n_dropped=sum(1 for c in final_constraints if c.scenario_role == ScenarioRole.DROPPED),
        n_merged=len(merged), min_shortlist=min_shortlist,
        persona_regenerations=regenerations, normalised=normalised, valid=bool(validity["valid"]),
        invalid_reasons=tuple(validity["reasons"]))


# ------------------------------------------------------------------ build vs dialogue truth consistency
def _build_truth_consistency(final_constraints, cands, alignments, normalised, doc_claims, kw, quantity,
                             build_cells) -> dict:
    """Re-evaluate the FINAL constraint list from scratch the way ``run_dialogue`` builds its truth state (alignments
    via ``alignment_dict``, roles read from each constraint's ``scenario_role``, the brief's own constraint ids) and
    compare every (product_id, constraint_id) cell status / ``scored`` flag with the cells the build used.

    ``build_truth_consistent`` / ``build_truth_mismatch_cells``: same evidence as the build (normalised included).
    ``build_truth_mismatch_cells_no_normalised``: the same with ``normalised=None``. ``build_truth_mismatch_cells`` is the
    key that matches the dialogue once ``run_dialogue`` receives ``BuiltScenario.normalised``; the no_normalised key is
    kept for the legacy call without it."""
    from gtgen.dialogue._util import alignment_dict
    from gtgen.matching import EvalContext
    active = [c for c in final_constraints if c.framing_decision not in _INACTIVE]
    adict = alignment_dict(alignments, strip_bid=False, pool=cands)
    ref = {(z.product_id, z.constraint_ref): (z.status, z.scored) for z in build_cells}

    def mismatches(norm) -> int:
        ctx = EvalContext(alignments=adict, normalised=norm, roles=None, doc_claims=doc_claims,
                          **kw)
        fresh = {(z.product_id, z.constraint_ref): (z.status, z.scored) for z in ctx.cells(active, cands)}
        return sum(1 for k in set(ref) | set(fresh) if ref.get(k) != fresh.get(k))

    n = mismatches(normalised)
    n_plain = mismatches(None) if normalised else n
    return {"build_truth_consistent": n == 0, "build_truth_mismatch_cells": int(n),
            "build_truth_mismatch_cells_no_normalised": int(n_plain)}


# ------------------------------------------------------------------ calibration (D050)
def _calibrate(settings, persona, profile, brief0, cands, al, normalised, roles, doc_claims, kw, quantity,
               min_shortlist, scenario_id, persona_seed, picks, bands, count_fn, max_steps, target, band, default,
               cells_for, roled_active, floors=None):
    """Pick the tightness candidate whose simulated top1_match_original lies in the persona tier's band.

    Candidates = states of the tightening walk (continued past the first one below the shortlist, up to
    ``max_tightening_steps``) whose initial verified-feasible count is < min_shortlist.  Returns the tuple
    (picks, tightness meta, cells, constraints, hardness map, count, calibration meta, qa flags)."""
    from gtgen.data.calibrate import calibrate as run_calibration, loosest_feasible, top1_match_original
    from gtgen.matching import (
        FrontTooLarge, EvalContext, config_from_settings, feasible_product_ids, hardness_map, params_from_persona,
        simulate_negotiation,
    )
    flag = str(settings["recommendation"].get("uncalibrated_flag", "uncalibrated"))
    d_picks, d_tmeta, d_cells, d_allc, d_hmap, d_after = default
    traj = tighten_trajectory(picks, bands, count_fn, max_steps, floors=floors, target=target,
                              min_shortlist=min_shortlist)
    points = [p for p in traj if p["count"] < min_shortlist]
    tier = persona.tier.value
    if not points:
        meta = {"tier": tier, "band": list(band), "n_candidates": 0, "candidates": [], "chosen_step": None,
                "chosen_top1_match_original": None, "in_band": False, "skipped": "no_candidate_below_min_shortlist",
                "calibration_assumes_perfect_beliefs": True}
        return d_picks, d_tmeta, d_cells, d_allc, d_hmap, d_after, meta, [flag]
    cfg = config_from_settings(settings)
    params = params_from_persona(persona, cfg)
    ectx = EvalContext(alignments=al, normalised=normalised, roles=roles, doc_claims=doc_claims, **kw)
    seed = f"{scenario_id}:calibration"

    def evaluate(p):
        cells_p, allc_p = cells_for(p["picks"])
        budget = float(p["picks"]["budget_inr"]) if "budget_inr" in p["picks"] else brief0.budget_inr
        loose = loosest_feasible(cells_p, hardness_map(allc_p))
        path = bool(loose >= min_shortlist)
        try:
            sim = simulate_negotiation(allc_p, cands, ectx, cfg, params, seed=seed, min_shortlist=min_shortlist, unit="supplier")
            top1, mo, outcome = top1_match_original(
                allc_p, sim.final_constraints, cands, ectx, quantity=quantity, budget_inr=budget,
                weights=persona.ranking_weights, buyer_city=profile.city, settings=settings,
                min_shortlist=min_shortlist)
        except (FrontTooLarge, RuntimeError) as exc:
            return {"error": f"{type(exc).__name__}: {str(exc)[:200]}", "top1_match_original": None,
                    "loosest_feasible": loose, "path_exists": path}
        return {"top1_match_original": mo, "loosest_feasible": loose, "path_exists": path, "top1_product_id": top1, "outcome": outcome,
                "stop_reason": sim.stop_reason, "feasible_initial": sim.feasible_initial,
                "feasible_final": sim.feasible_final, "n_rounds": len(sim.rows)}

    idx, meta = run_calibration(points, evaluate, band, tier)
    chosen = points[idx]
    cells_c, allc_c = cells_for(chosen["picks"])
    hmap_c = hardness_map(allc_c)
    after_c = len(feasible_product_ids(cells_c, hmap_c))
    tm = dict(d_tmeta)
    tm.update({"steps": int(chosen["step"]), "terms_tightened": list(chosen["terms_tightened"]),
               "count_after": int(chosen["count"]), "reached_below_min_shortlist": True,
               "target_reached": bool(chosen["count"] <= target), "calibrated": True})
    flags = [] if meta["in_band"] else [flag]
    return chosen["picks"], tm, cells_c, allc_c, hmap_c, after_c, meta, flags
