"""Turn loop: Buyer opening, then Negotiator step / Buyer response until an end condition (D053, D055, D056).

The loop talks to the negotiator ONLY through ``step(TranscriptView, CatalogueView)`` (D030); the buyer's hidden
per-item decisions never reach it (D045). The negotiator re-derives bounds and refusals from the transcript through
``negotiator_interpret`` (the belief loop); the truth loop is driven by ``matching.buyer_decide`` /
``apply_decision`` inside ``BuyerSimulator``. Optional read-only hooks (GT negotiator only, absent hooks are fine):
``drain_qa_flags``, ``last_cells`` and ``last_analysis`` (its own front / bounds / verified counts, for the log).

Per negotiator turn one ``RelaxationAnalysisRow`` is logged: front points, chosen point, bounds before / after for the
truth and the belief state, verified (feasible product) counts before / after for both, the buyer's per-item decision
and ``agrees_with_reference``. The belief "after" columns are filled when the next negotiator step has re-derived its
state from the transcript.

D077: the negotiator never clarifies; it goes straight to the relaxation loop (mandatory below the shortlist), then the
final turn (RECOMMEND / BEST_AVAILABLE), which closes the conversation (no buyer turn follows). Each row carries the per-constraint
attempt ledger (``constraint_attempts``).

Ends: shortlist reached (RECOMMEND), buyer patience (``persona.patience_rounds`` rounds without a YES) or the turn cap
(one public WRAP_UP turn, then the negotiator's final RECOMMEND / BEST_AVAILABLE), or an empty front (BEST_AVAILABLE,
D050). A diagnostic front (spec 8.7) is logged in the row but never proposed: it cannot change any bound, so the
outcome equals ``matching.simulate_negotiation`` (which calibration uses) apart from D055 proof-request draws, which
consume the same buyer rng.
"""
from __future__ import annotations

import collections
import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from gtgen.contracts import (
    Action, AttributeAlignment, BelievedConstraint, BuyerConstraint, BuyerProfile, CatalogueView,
    ComplianceCell, DocClaim, NegotiatorStep, NegotiatorUnderTest, Persona, ProofRequest, RankingRow,
    RelaxationAnalysisRow, RelaxationOption, ScenarioBrief, Speaker, TranscriptTurn, TranscriptView, Turn,
)
from gtgen.matching import EvalContext, compliance_matrix, config_from_settings
from gtgen.matching.compliance import is_inactive
from gtgen.llm.runners import humanise_label

from ._truth import TruthState
from ._util import alignment_dict, feasible_ids, norm_attr, overlap_ratio, per_belief_alignments, value_overlap
from .buyer import BuyerSimulator
from ._best import best_available_ranking, best_available_view
from .negotiator import Negotiator, catalogue_term, extract_quantity_budget
from .wrap_up import WRAP_UP_TEXT

_FINAL = {Action.RECOMMEND: "RECOMMENDED", Action.BEST_AVAILABLE: "BEST_AVAILABLE"}
MAX_CONSECUTIVE_ACKS = 2

def negotiator_cache_seed(persona_seed: int) -> int:
    """One-way derived seed for everything the negotiator side touches (ConversationContext, cache key, log).

    The real ``persona_seed`` seeds the buyer's random stream; it must never reach the negotiator (D061, W3).
    Deterministic, so warm caches stay valid; unique per persona_seed (48-bit digest prefix).
    """
    return int(hashlib.sha256(f"{persona_seed}:negotiator".encode()).hexdigest()[:12], 16)


@dataclass
class DialogueState:
    turns: list[Turn]
    outcome: str  # "RECOMMENDED" | "BEST_AVAILABLE" (D050: a conversation always ends with a recommendation)
    brief: ScenarioBrief
    persona: Persona
    buyer_profile: BuyerProfile
    catalogue: CatalogueView
    alignments: list[AttributeAlignment]
    accepted_relaxations: list[RelaxationOption]
    final_ranking: Optional[list[RankingRow]]
    qa_flags: list[str]
    truth_cells: list[ComplianceCell] = field(default_factory=list)
    belief_cells: list[ComplianceCell] = field(default_factory=list)
    final_beliefs: list[BelievedConstraint] = field(default_factory=list)
    # D052: the truth state before any concession and after the agreed ones
    initial_constraints: list[BuyerConstraint] = field(default_factory=list)
    initial_cells: list[ComplianceCell] = field(default_factory=list)
    final_constraints: list[BuyerConstraint] = field(default_factory=list)
    bypassed: list[str] = field(default_factory=list)
    scenario_mode: bool = False
    offered_product_ids: list[str] = field(default_factory=list)   # D050: the BEST_AVAILABLE offer, in offer order
    best_available_reason: Optional[str] = None      # "no_feasible" | "patience_exhausted" (outcome BEST_AVAILABLE only)
    relaxation_analysis: list[RelaxationAnalysisRow] = field(default_factory=list)   # D056
    proof_requests: list[ProofRequest] = field(default_factory=list)                 # D055
    truth_bounds: dict = field(default_factory=dict)                                 # final truth bounds
    llm_provenance: dict = field(default_factory=dict)                               # D054 live / cache_hit summary
    claim_cache_stats: dict = field(default_factory=dict)                            # D071 dialogue doc_claim pairs: hits / sent / dedup
    role_reasons: dict = field(default_factory=dict)                                  # belief ref -> zero_pass | low_coverage
    constraint_attempts: list[dict] = field(default_factory=list)                    # D072 ledger per negotiator turn (until RelaxationAnalysisRow has the field)


def _value_text(c: BuyerConstraint) -> str:
    return c.buyer_value or ",".join(c.allowed_values or [])


def _pair_score(belief: BelievedConstraint, c: BuyerConstraint) -> float:
    """3.0 exact normalised name (attribute, then spec_name); 2.5 synthetic term by wording (D049); else the Dice
    overlap (>= 0.5) of the stemmed name tokens plus a 0.05 value tie-break; 0.0 = no match."""
    n = norm_attr(belief.attribute)
    if n and norm_attr(c.attribute) == n:
        return 3.0
    if n and c.spec_name and norm_attr(c.spec_name) == n:
        return 2.9
    term = catalogue_term(belief)
    if term is not None and c.provenance == "synthetic_commercial" and c.attribute == term:
        return 2.5
    r = max(overlap_ratio(belief.attribute, c.attribute), overlap_ratio(belief.attribute, c.spec_name))
    if r < 0.5:
        return 0.0
    return r + 0.05 * value_overlap(belief.value, _value_text(c))


def match_beliefs(beliefs: list[BelievedConstraint], constraints: list[BuyerConstraint],
                  prior: Optional[dict[str, str]] = None) -> dict[str, Optional[BuyerConstraint]]:
    """belief_id -> truth constraint, GT-side labelling only (audit r2 defect 2).

    * most specific name wins (Dice token overlap, stemmed; "strap material" -> "strap tape webbing material", not
      "inside material"); exact names and synthetic terms first;
    * one-to-one: two beliefs never claim the same constraint (greedy by score; stable order on ties);
    * stable across turns: a pair in ``prior`` (belief_id -> constraint_id) that still scores > 0 is kept first.
    """
    by_id = {c.constraint_id: c for c in constraints}
    pairs = [(sc, i, j) for i, b in enumerate(beliefs) for j, c in enumerate(constraints)
             if (sc := _pair_score(b, c)) > 0]
    out: dict[str, Optional[BuyerConstraint]] = {b.belief_id: None for b in beliefs}
    taken: set[str] = set()
    for i, b in enumerate(beliefs):
        cid = (prior or {}).get(b.belief_id)
        if cid in by_id and cid not in taken and _pair_score(b, by_id[cid]) > 0:
            out[b.belief_id] = by_id[cid]
            taken.add(cid)
    for sc, i, j in sorted(pairs, key=lambda p: (-p[0], p[1], p[2])):
        b, c = beliefs[i], constraints[j]
        if out[b.belief_id] is None and c.constraint_id not in taken:
            out[b.belief_id] = c
            taken.add(c.constraint_id)
    return out


def match_belief(belief: BelievedConstraint, constraints: list[BuyerConstraint]) -> Optional[BuyerConstraint]:
    """Single-belief form of ``match_beliefs`` (most specific name, exact first)."""
    return match_beliefs([belief], constraints)[belief.belief_id]


def _transcript(scenario_id: str, turns: list[Turn]) -> TranscriptView:
    return TranscriptView(scenario_id=scenario_id, turns=[
        TranscriptTurn(turn_idx=t.turn_idx, speaker=t.speaker, utterance=t.utterance) for t in turns])


def _product_category(catalogue: CatalogueView) -> Optional[str]:
    cnt = collections.Counter(c.category_name for c in catalogue.candidates if c.category_name)
    return cnt.most_common(1)[0][0] if cnt else None


def run_dialogue(brief: ScenarioBrief, persona: Persona, buyer_profile: BuyerProfile, catalogue: CatalogueView,
                 alignments: list[AttributeAlignment], runners: Any, settings: dict, *,
                 negotiator: Optional[NegotiatorUnderTest] = None,
                 product_category: Optional[str] = None,
                 doc_claims: Optional[Mapping[tuple[str, str], DocClaim]] = None,
                 scenario_mode: Optional[bool] = None,
                 sales_persona: Optional[dict] = None, claim_cache: Any = None,
                 normalised: Optional[Mapping] = None) -> DialogueState:
    """Run one negotiation (the D056 relaxation loop).

    ``doc_claims``: truth-side claim records keyed (product_id, constraint_id) (D051); ``None`` keeps the
    evidence-based claim detection of the matching module.

    ``normalised``: truth-side normalised values keyed (product_id, buyer attribute) (BuiltScenario.normalised); used ONLY
    by the truth EvalContext, never given to the negotiator (it keeps its own normalisation from the catalogue).

    ``scenario_mode`` only switches the GT negotiator's D048 verification-only roles (``None`` = automatic: on iff the
    brief carries scenario roles, a ``scenario_meta`` or ``doc_claims``). The truth state ALWAYS evolves with the
    agreed relaxations.

    ``sales_persona``: the negotiator's own persona (D054); default ``brief.scenario_meta["sales_persona"]``.

    ``claim_cache``: the table stage's ``BuiltScenario.claim_cache`` (D071). Pairs already classified there are not
    sent to the doc_claim LLM again; ``None`` = nothing known (still deduplicated). Hit/sent counts: ``state.claim_cache_stats``.
    """
    neg_cfg = settings["negotiation"]
    hard_cap = int(neg_cfg["max_turns_hard_cap"])
    unverified = float(settings["ranking"]["unverified_text_factor"])
    rel_tol = float(neg_cfg["bundle_search"]["numeric_eq_rel_tolerance"])
    vo_fail_blocks = bool(settings["scenario"]["verification_only_fail_blocks"])
    alignments = list(alignments or [])
    adict = alignment_dict(alignments, strip_bid=False, pool=catalogue)
    if scenario_mode is None:
        scenario_mode = (any(c.scenario_role is not None for c in brief.constraints) or bool(brief.scenario_meta)
                         or doc_claims is not None)
    sales = sales_persona if sales_persona is not None else brief.scenario_meta.get("sales_persona")

    # ---- truth side: rules, bounds and the derived view (D052/D053); active = brief minus DROP / MERGE / DROPPED
    active = [c for c in brief.constraints if not is_inactive(c)]
    tctx = EvalContext(alignments=adict, normalised=normalised, doc_claims=doc_claims, quantity=brief.quantity,
                       verification_only_fail_blocks=vo_fail_blocks, unverified_factor=unverified, rel_tol=rel_tol)
    truth = TruthState(active, catalogue.candidates, tctx, config_from_settings(settings))
    buyer = BuyerSimulator(brief, persona, buyer_profile, runners, settings,
                           product_category=product_category or _product_category(catalogue), truth=truth)
    if negotiator is None:
        negotiator = Negotiator(catalogue, runners, settings, alignments=alignments,
                                verification_roles=scenario_mode, sales_persona=sales,
                                cache_seed=negotiator_cache_seed(persona.persona_seed), claim_cache=claim_cache)
    patience = buyer.params.patience_rounds

    state = DialogueState(turns=[], outcome="BEST_AVAILABLE", brief=brief, persona=persona,
                          buyer_profile=buyer_profile, catalogue=catalogue, alignments=alignments,
                          accepted_relaxations=[], final_ranking=None, qa_flags=[], truth_cells=truth.cells,
                          initial_constraints=truth.initial_constraints, initial_cells=truth.initial_cells,
                          final_constraints=truth.constraints, scenario_mode=bool(scenario_mode))

    def refresh_state() -> None:
        state.truth_cells, state.final_constraints = truth.cells, truth.constraints
        state.bypassed = list(truth.state.bypassed)
        state.truth_bounds = truth.bounds

    def drain() -> None:
        state.qa_flags.extend(buyer.drain_qa_flags())
        state.qa_flags.extend(getattr(negotiator, "drain_qa_flags", lambda: [])())

    # ---- turn 0: buyer opening
    utt, revealed, ids = buyer.opening(seed=persona.persona_seed, turn_idx=0)
    state.turns.append(Turn(turn_idx=0, speaker=Speaker.BUYER, action=Action.OPEN_REQUEST, utterance=utt,
                            revealed_constraint_ids=revealed, buyer_truth_state_after=list(truth.constraints),
                            feasible_count_truth=len(truth.feasible), feasible_count_belief=0, llm_call_ids=ids))
    drain()

    last_beliefs: list[BelievedConstraint] = []
    last_belief_feasible = 0
    ever_beliefs: list[BelievedConstraint] = []     # the latest non-empty belief list (cut-off fallback)
    wrapped = False                      # the public WRAP_UP turn has been appended (D050 amendment)
    sticky: dict[str, str] = {}          # belief_id -> constraint_id: the GT-side label of a belief stays stable across turns
    dry = 0                              # consecutive buyer rounds without a YES (patience)
    acks = 0
    awaiting: Optional[RelaxationAnalysisRow] = None     # PROPOSE row whose belief "after" columns are still open

    def wrap_up(*, cap: bool) -> None:
        """Patience or the turn cap ran out: ONE public, deterministic buyer turn asks for the final offer. It and the
        negotiator's answer are exempt from ``max_turns_hard_cap`` (they are the end of the conversation)."""
        nonlocal wrapped
        if cap:
            state.qa_flags.append("hard_turn_cap_reached")
        state.turns.append(Turn(
            turn_idx=len(state.turns), speaker=Speaker.BUYER, action=Action.WRAP_UP, utterance=WRAP_UP_TEXT,
            buyer_truth_state_after=list(truth.constraints), negotiator_belief_state=last_beliefs,
            feasible_count_truth=len(truth.feasible), feasible_count_belief=last_belief_feasible))
        wrapped = True

    while True:
        if not wrapped and len(state.turns) >= hard_cap:
            wrap_up(cap=True)
        transcript = _transcript(catalogue.scenario_id, state.turns)
        step: NegotiatorStep = negotiator.step(transcript, catalogue)
        drain()

        # belief-side compliance: the GT negotiator exposes the cells it computed (roles, claims included);
        # any other negotiator gets a plain recomputation (barrier-neutral)
        beliefs = list(step.beliefs)
        b_cells = getattr(negotiator, "last_cells", None)
        if not (isinstance(b_cells, list) and {c.constraint_ref for c in b_cells} <= {b.belief_id for b in beliefs}):
            b_cells = compliance_matrix(beliefs, catalogue.candidates, per_belief_alignments(beliefs, adict),
                                        unverified_factor=unverified, rel_tol=rel_tol)
        b_feasible = len(feasible_ids(beliefs, catalogue.candidates, b_cells))
        state.belief_cells = b_cells

        resolved: dict[str, Optional[BuyerConstraint]] = {}
        stamped: list[BelievedConstraint] = []
        matched = match_beliefs(beliefs, active, prior=sticky)
        for b in beliefs:
            m = matched[b.belief_id]
            resolved[b.belief_id] = m
            if m is not None:
                sticky[b.belief_id] = m.constraint_id
            stamped.append(b.model_copy(update={"matched_constraint_id": m.constraint_id if m else None}))
        last_beliefs, last_belief_feasible = stamped, b_feasible
        if stamped:
            ever_beliefs = beliefs
        state.final_beliefs = stamped

        an = getattr(negotiator, "last_analysis", None)
        an = an if isinstance(an, dict) else {}
        b_bounds = {k: dict(v) for k, v in (an.get("bounds_before") or {}).items()}
        b_ver = int(an.get("verified_before", b_feasible))
        if awaiting is not None:                                  # the belief state after the buyer's last answer
            awaiting.bounds_after_belief, awaiting.verified_after_belief = b_bounds, b_ver
            awaiting = None

        idx = len(state.turns)
        state.turns.append(Turn(
            turn_idx=idx, speaker=Speaker.NEGOTIATOR, action=step.action, utterance=step.utterance,
            proposal=step.proposal, buyer_truth_state_after=list(truth.constraints),
            negotiator_belief_state=stamped, feasible_count_truth=len(truth.feasible),
            feasible_count_belief=b_feasible, llm_call_ids=list(step.llm_call_ids)))
        row = RelaxationAnalysisRow(
            turn_idx=idx, iteration=len(state.relaxation_analysis), front_points=list(an.get("front_points") or []),
            chosen_point=an.get("chosen_point"), chosen_action=step.action.value,
            bounds_before_truth=truth.bounds, bounds_after_truth=truth.bounds,
            bounds_before_belief=b_bounds, bounds_after_belief=dict(b_bounds),
            verified_before_truth=len(truth.feasible), verified_after_truth=len(truth.feasible),
            verified_before_belief=b_ver, verified_after_belief=b_ver, agrees_with_reference=an.get("agrees"))
        for ref, why in (an.get("belief_role_reasons") or {}).items():    # r6: reasons are recorded on EVERY turn
            state.role_reasons[ref] = why
        attempts = an.get("constraint_attempts")
        if attempts:       # D072: how far the relaxation loop got on every stated constraint (auditable per row)
            state.constraint_attempts.append({"turn_idx": idx, "action": step.action.value, "attempts": attempts,
                                              "loop_exhausted": an.get("loop_exhausted")})
            row.constraint_attempts = {**attempts, "loop_exhausted": bool(an.get("loop_exhausted"))}   # D072: GT-visible
        state.relaxation_analysis.append(row)

        if step.action in _FINAL:
            state.outcome = _FINAL[step.action]
            state.final_ranking = step.ranking
            if step.action == Action.BEST_AVAILABLE:
                state.offered_product_ids = list(step.offered_product_ids)
                state.best_available_reason = "patience_exhausted" if wrapped else (
                    an.get("best_available_reason") or "no_feasible")           # r6: 'nothing_verified' (D050 note)
            break
        if wrapped:
            # a negotiator under test that does not answer the wrap-up with an offer is cut off: the final
            # BEST_AVAILABLE is built from the last belief-side ranking the loop can compute (barrier-neutral)
            state.qa_flags.append("negotiator_ignored_wrap_up")
            bl = beliefs or ever_beliefs
            cells = compliance_matrix(bl, catalogue.candidates, per_belief_alignments(bl, adict),
                                      unverified_factor=unverified, rel_tol=rel_tol)
            qty, _u, budget = extract_quantity_budget(transcript)
            state.final_ranking = best_available_ranking(bl, bl, catalogue.candidates, cells, cells, settings,
                                                         quantity=qty, budget_inr=budget)
            state.offered_product_ids = [i.product_id for i in
                                         best_available_view(state.final_ranking, bl, cells, cells, settings).items]
            state.outcome, state.best_available_reason = "BEST_AVAILABLE", "patience_exhausted"
            break
        if step.action == Action.ACKNOWLEDGE:      # D055: the negotiator confirmed a proof request; it goes on
            acks += 1
            if acks > MAX_CONSECUTIVE_ACKS:
                state.qa_flags.append(f"too_many_acknowledgements:{idx}")
                wrap_up(cap=False)
            continue
        acks = 0
        if step.action != Action.PROPOSE_RELAXATION or step.proposal is None or step.proposal.bundle is None:
            # D077: the negotiator never emits CLARIFY any more; this is any other action with no buyer policy.
            state.qa_flags.append(f"unhandled_negotiator_action:{idx}:{step.action.value}")
            continue
        if len(state.turns) >= hard_cap:
            wrap_up(cap=True)
            continue

        bundle = step.proposal.bundle
        # D078 fix: a synthetic/commercial belief (budget_inr, qty_vs_moq, preferred_states, ...) has no matching
        # BuyerConstraint, so BuyerSimulator falls back to this label when speaking about it - the raw internal
        # attribute name (e.g. "preferred_states") must never reach the buyer_reply prompt verbatim.
        labels = {b.belief_id: humanise_label(b.attribute) for b in beliefs}
        transcript = _transcript(catalogue.scenario_id, state.turns)
        turn_idx = len(state.turns)
        resp = buyer.respond_to_proposal(bundle, resolved, transcript, seed=persona.persona_seed + turn_idx,
                                         turn_idx=turn_idx, item_labels=labels)
        drain()
        refresh_state()
        # D044: a proposal that touches unrevealed constraints is answered in the SAME buyer turn by the reveal
        # sentence; the turn action stays the proposal action, reveal_check runs once over the joined text.
        utt, new_ids, rids = buyer.reveal(step.utterance, Action.PROPOSE_RELAXATION, transcript,
                                          seed=persona.persona_seed + turn_idx + 1, lead_text=resp.utterance,
                                          turn_idx=turn_idx)
        drain()
        state.accepted_relaxations.extend(resp.accepted)
        state.proof_requests.extend(resp.proof_requests)
        state.turns.append(Turn(
            turn_idx=turn_idx, speaker=Speaker.BUYER, action=resp.action, utterance=utt,
            proposal_outcome=resp.outcome, counter_value=resp.counter_value, item_responses=resp.responses,
            proof_requests=resp.proof_requests, revealed_constraint_ids=new_ids,
            buyer_truth_state_after=list(truth.constraints), negotiator_belief_state=last_beliefs,
            feasible_count_truth=len(truth.feasible), feasible_count_belief=last_belief_feasible,
            llm_call_ids=resp.llm_call_ids + rids))
        row.buyer_decision = resp.decision
        row.bounds_after_truth, row.verified_after_truth = truth.bounds, len(truth.feasible)
        awaiting = row
        dry = 0 if resp.yes else dry + 1
        if dry >= patience:
            wrap_up(cap=False)
    refresh_state()
    state.claim_cache_stats = dict(getattr(negotiator, "claim_cache_stats", None) or {})
    summary = getattr(getattr(runners, "client", None), "provenance_summary", None)
    if callable(summary):
        state.llm_provenance = summary(brief.scenario_id)
    return state
