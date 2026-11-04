"""Conversation-level evaluation over GTConversation records (out/gt/<run_id>/conversations.jsonl).

Definitions (documented because they are label semantics):
* truth-feasible row  : eligible (no HARD FAIL/UNKNOWN) and soft_fail == 0.
* belief-feasible row : same predicate on the negotiator ranking.
* relevant set        : product_ids truth-feasible in ``reference_ranking``; gains = reference score.
* recommended         : the first ``shortlist_k`` belief-feasible rows of ``negotiator_ranking`` (rank order),
                        only when outcome == RECOMMENDED.
* Brier / ECE         : negotiator p_feasible vs truth-feasibility, paired by product_id.
* false-feasibility   : among belief-feasible rows, share that are not truth-feasible.
* hard-violation rate : among recommended, share with truth hard_fail > 0.
* policy compliance   : D053 replay.  Buyer turns that answer a negotiator proposal; each item response must be the
  action ``matching.buyer_decide`` returns for the item (persona -> ``params_from_persona``; the proposed values ->
  levels of the buyer's rule; bounds before the turn from ``relaxation_analysis[].bounds_before_truth``), with the
  buyer rng ``buyer_rng(persona_seed)`` consumed in turn order.  Unmatched belief -> YES; no draw is needed for
  doc-proof (BYPASS), HARD / non-relaxable (NO), limit, tolerance (COUNTER/NO) and bounded items.  The probabilistic
  YES/NO step is replayed exactly while the rng is in sync; the D055 proof-request draws that follow a YES consume an
  unknown number of values, so after such a turn (proof_requests enabled in ``settings``) the step is only required to
  be YES or NO unless the recorded ``buyer_decision`` carries ``u`` and ``p_accept``.
* state-transition accuracy: buyer turns whose (action, proposal_outcome) equal those derived from
  item_responses (all YES->YES/ACCEPTED, all NO->NO/REJECTED, all BYPASS->BYPASS/BYPASSED, all COUNTER->
  COUNTER/COUNTERED, otherwise MIXED/PARTIAL; a mixed set with a single non-NO kind is still MIXED).
* D050 additions: best_available_share (BEST_AVAILABLE conversations / all); per_tier (persona.tier): perfect-deal
  share from top1_perfect_deal and the top1_match_original distribution; hard-violation rate counts BEST_AVAILABLE
  items with non_compliant=True (denominator = all best-available items); verification_required_coverage = share
  of top-1 recommendations (RECOMMENDED: first recommended negotiator row; BEST_AVAILABLE: first item) with a
  non-empty verification_required list.  No band/target is read here (see metrics.share_in_band).
* D050 amendment: ``best_available_reason_share`` = share of BEST_AVAILABLE conversations per reason (no_feasible,
  patience_exhausted, nothing_verified); ``wrap_up_share`` = share of all conversations containing a WRAP_UP turn.  Catalogue coverage
  counts the products recommended (RECOMMENDED: shortlist rows; BEST_AVAILABLE: the offered items).
* policy compliance resolves a bundle item's belief id to its truth constraint through the proposing negotiator
  turn's ``negotiator_belief_state[].matched_constraint_id`` (nearest earlier negotiator turn as fallback), reads the
  truth constraint from ``buyer_truth_state_after`` at that turn (D052); an unmatched belief is a YES.
* turns to agreement  : turn index (1-based count) of the RECOMMEND turn, for RECOMMENDED outcomes.
Barrier-leak and unsupported-claim metrics need text analysis and the hidden state; they belong to the
gt-auditor and are not computed here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

from gtgen.contracts import (
    Action,
    GTConversation,
    Hardness,
    ItemResponse,
    RankingRow,
    Speaker,
    Tier,
    Turn,
)
from gtgen.dialogue._relax import change_fraction, level_ceil
from gtgen.eval import metrics as M
from gtgen.eval import persona_diversity as PD
from gtgen.eval import realism as RL
from gtgen.eval import relaxation_metrics as RM
from gtgen.matching import (
    ProposalItem, baseline_levels, build_rules, buyer_decide, buyer_rng, config_from_settings, domain_levels,
    params_from_persona, is_state_rule, state_level_for,
)
from gtgen.contracts import CandidateView as _CandidateView


def _pool_candidates(conv) -> Optional[list]:
    """Minimal stubs (state only) of the pool from ``scenario_meta['pool_states']``; None when not recorded."""
    ps = (getattr(conv.commercial_brief, "scenario_meta", None) or {}).get("pool_states")
    if not ps:
        return None
    return [_CandidateView(product_id=f"p{i}", supplier_id=f"s{i}", product_name="", state=s)
            for i, s in enumerate(ps)]


def load_conversations(path: str | Path) -> list[GTConversation]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(GTConversation.model_validate(json.loads(line)))
    return out


def truth_feasible(row: RankingRow) -> bool:
    return row.eligible and row.soft_fail == 0


def ranked_ids(rows: Sequence[RankingRow], only_eligible: bool = True) -> list[str]:
    rs = sorted(rows, key=lambda r: r.rank)
    return [r.product_id for r in rs if (r.eligible or not only_eligible)]


def ranking_metrics(negotiator: Sequence[RankingRow], reference: Sequence[RankingRow],
                    ks: Sequence[int]) -> dict[str, Optional[float]]:
    ref_ids = ranked_ids(reference)
    neg_ids = ranked_ids(negotiator)
    relevant = {r.product_id for r in reference if truth_feasible(r)}
    gains = {r.product_id: r.score for r in reference if r.eligible}
    out: dict[str, Optional[float]] = {}
    for k in ks:
        out[f"ndcg@{k}"] = M.ndcg_at_k(neg_ids, gains, k)
        out[f"precision@{k}"] = M.precision_at_k(neg_ids, relevant, k)
        out[f"recall@{k}"] = M.recall_at_k(neg_ids, relevant, k)
    out["mrr"] = M.reciprocal_rank(neg_ids, relevant)
    out["map"] = M.average_precision(neg_ids, relevant)
    out["kendall_tau"] = M.kendall_tau(neg_ids, ref_ids)
    return out


def calibration(negotiator: Sequence[RankingRow], reference: Sequence[RankingRow],
                n_bins: int = 10) -> tuple[list[float], list[bool]]:
    truth = {r.product_id: truth_feasible(r) for r in reference}
    probs, outs = [], []
    for r in sorted(negotiator, key=lambda r: r.product_id):
        if r.product_id in truth:
            probs.append(r.p_feasible)
            outs.append(truth[r.product_id])
    return probs, outs


def recommended_rows(conv: GTConversation, shortlist_k: int) -> list[RankingRow]:
    if conv.final.outcome != "RECOMMENDED":
        return []
    rows = [r for r in sorted(conv.final.negotiator_ranking, key=lambda r: r.rank) if truth_feasible(r)]
    return rows[:shortlist_k]


def false_feasibility(conv: GTConversation) -> tuple[int, int]:
    """(belief-feasible but truth-infeasible, belief-feasible) over rows present in both rankings."""
    truth = {r.product_id: truth_feasible(r) for r in conv.final.reference_ranking}
    bf = [r for r in conv.final.negotiator_ranking if truth_feasible(r) and r.product_id in truth]
    return sum(1 for r in bf if not truth[r.product_id]), len(bf)


def hard_violations(conv: GTConversation, shortlist_k: int) -> tuple[int, int]:
    """(violations, recommended).  RECOMMENDED: recommended rows with a truth hard_fail.  BEST_AVAILABLE (D050):
    items flagged ``non_compliant`` over all items."""
    if conv.final.outcome == "BEST_AVAILABLE":
        items = conv.final.best_available.items if conv.final.best_available else []
        return sum(1 for it in items if it.non_compliant), len(items)
    rec = recommended_rows(conv, shortlist_k)
    ref = {r.product_id: r for r in conv.final.reference_ranking}
    bad = sum(1 for r in rec if r.product_id in ref and ref[r.product_id].hard_fail > 0)
    return bad, len(rec)


# ---------------------------------------------------------------- policy / transitions
def derive_turn_labels(responses: Sequence[ItemResponse]) -> tuple[Optional[Action], Optional[str]]:
    acts = {r.action for r in responses}
    if not acts:
        return None, None
    if acts == {Action.YES}:
        return Action.YES, "ACCEPTED"
    if acts == {Action.NO}:
        return Action.NO, "REJECTED"
    if acts == {Action.BYPASS}:
        return Action.BYPASS, "BYPASSED"
    if acts == {Action.COUNTER}:
        return Action.COUNTER, "COUNTERED"
    return Action.MIXED, "PARTIAL"


def state_transition_ok(turn: Turn) -> bool:
    # D055: proof-request answers (BYPASS rows) are appended AFTER the turn action was derived; leave them out
    n_proof = len(turn.proof_requests)
    resp = turn.item_responses[:len(turn.item_responses) - n_proof] if n_proof else turn.item_responses
    action, outcome = derive_turn_labels(resp)
    return turn.action == action and turn.proposal_outcome == outcome


def _proposal_items(prev: Turn):
    if prev.proposal is None:
        return []
    if prev.proposal.bundle is not None:
        return list(prev.proposal.bundle.items)
    if prev.proposal.option is not None:
        return [prev.proposal.option]
    return []


def _turn_position(conv: GTConversation, turn: Turn) -> int:
    for i, t in enumerate(conv.turns):
        if t is turn:
            return i
    for i, t in enumerate(conv.turns):
        if t.turn_idx == turn.turn_idx and t.speaker == turn.speaker:
            return i
    return len(conv.turns)


def resolve_ref(conv: GTConversation, prev: Turn, ref: str) -> tuple[bool, Optional[str]]:
    """Map a bundle item's ``constraint_ref`` (a negotiator BELIEF id) to a truth constraint id.

    Looks in ``prev.negotiator_belief_state`` (the negotiator turn that made the proposal), then in the nearest
    earlier negotiator turn whose belief state contains the ref.  Returns (found, matched_constraint_id):
    found and matched None = the loop's "unmatched belief".  Not found anywhere = the ref is not a belief id; it is
    then looked up as a truth id (hand-built conversations that propose truth ids directly)."""
    pos = _turn_position(conv, prev)
    chain = [prev] + [t for t in reversed(conv.turns[:pos]) if t.speaker == Speaker.NEGOTIATOR]
    for t in chain:
        for b in t.negotiator_belief_state:
            if b.belief_id == ref:
                return True, b.matched_constraint_id
    return False, ref


def truth_constraint_at(conv: GTConversation, prev: Turn, cid: Optional[str]):
    """The truth constraint ``cid`` as of the proposal turn (D052: the truth state evolves); falls back to the
    nearest earlier turn that recorded a truth state, then to the brief."""
    if cid is None:
        return None
    pos = _turn_position(conv, prev)
    for t in [prev] + list(reversed(conv.turns[:pos])):
        if t.buyer_truth_state_after:
            for c in t.buyer_truth_state_after:
                if c.constraint_id == cid:
                    return c
            break
    return next((c for c in conv.commercial_brief.constraints if c.constraint_id == cid), None)


def _analysis_row(conv: GTConversation, prev: Turn):
    return next((r for r in conv.relaxation_analysis if r.turn_idx == prev.turn_idx), None)


def _proof_enabled(settings: Optional[Mapping]) -> bool:
    return bool(((settings or {}).get("proof_requests", {}) or {}).get("enabled", False))


def expected_responses(conv: GTConversation, prev: Turn, settings: Optional[Mapping], rng, synced: bool
                       ) -> tuple[dict[str, set[Action]], bool]:
    """Replay of the D053 buyer for the proposal made at ``prev`` -> ({proposal constraint_ref: allowed actions},
    stays_synced).  Mirrors ``dialogue.buyer.BuyerSimulator.respond_to_proposal``: items are matched to truth
    constraints, mapped to a level of the buyer's own rule through the relative change they ask for, and decided by
    ``matching.buyer_decide`` with the persona parameters and the SHARED buyer rng (``rng``; one draw per item that
    reaches the probabilistic step, sorted by constraint id).  Bounds before the turn come from the matching
    ``relaxation_analysis`` row (``bounds_before_truth``; empty when the conversation has none).

    ``synced`` False (a proof-request draw of an earlier turn consumed an unknown number of values from the rng) =
    the probabilistic step cannot be replayed: its allowed set is {YES, NO}, unless the row's recorded
    ``buyer_decision`` carries ``u`` / ``p_accept`` (then it is checked exactly).  Everything else is exact."""
    cfg = config_from_settings(settings)
    params = params_from_persona(conv.persona, cfg)
    originals = list(conv.commercial_brief.constraints)
    roles = {c.constraint_id: c.scenario_role for c in originals}
    rules = build_rules(originals, cfg, roles=roles, candidates=_pool_candidates(conv))
    by_ref = {r.ref: r for r in rules}
    row = _analysis_row(conv, prev)
    bounds = {k: dict(v) for k, v in ((row.bounds_before_truth if row else None) or {}).items()}
    base = baseline_levels(rules, bounds)
    recorded = (row.buyer_decision if row else None) or {}

    allowed: dict[str, set[Action]] = {}
    decide_items: list[ProposalItem] = []
    not_permitted: list[str] = []
    seen: dict[str, str] = {}
    entries: list[tuple[str, Optional[str], object, Optional[int]]] = []     # (belief ref, truth id, rule, level)
    for opt in _proposal_items(prev):
        ref = opt.constraint_ref
        found, cid = resolve_ref(conv, prev, ref)
        if found and cid is None:
            allowed[ref] = {Action.YES}                                   # unmatched belief
            continue
        tc = truth_constraint_at(conv, prev, cid)
        if tc is None:
            allowed[ref] = {Action.NO}
            continue
        if tc.constraint_id in seen:
            entries.append((ref, tc.constraint_id, "dup", None))
            continue
        seen[tc.constraint_id] = ref
        rule = by_ref.get(tc.constraint_id)
        if rule is None and is_state_rule(tc):
            # D076: the pool's states are needed to rebuild the state rule (``scenario_meta['pool_states']``); without
            # them the answer cannot be replayed, so any of the three item answers is accepted.
            allowed[ref] = {Action.YES, Action.NO, Action.COUNTER}
            continue
        if rule is None:
            decide_items.append(ProposalItem(ref=tc.constraint_id, level=1, max_level=1, hardness=tc.hardness,
                                             category=tc.constraint_category, doc_proof=tc.is_doc_proof))
            if not tc.is_doc_proof:
                not_permitted.append(tc.constraint_id)
            entries.append((ref, tc.constraint_id, None, None))
            continue
        if rule.mode == "set":                                           # D076: state widening
            r = state_level_for(rule, opt.to_value)
        else:
            frac = change_fraction(opt.from_value, opt.to_value)
            r = level_ceil(rule.levels, frac) if frac is not None else None
        cur = base[rule.ref]
        entries.append((ref, tc.constraint_id, rule, r))
        if r is None:
            allowed[ref] = {Action.NO}
        elif r <= cur:
            allowed[ref] = {Action.YES}
        elif r not in domain_levels(rule, bounds):
            allowed[ref] = {Action.NO}
        else:
            decide_items.append(ProposalItem(ref=rule.ref, level=r, max_level=rule.top, hardness=rule.hardness,
                                             category=rule.category, current_level=cur))
    accepted_rules = [k for k, b in bounds.items() if "fixed" in b]
    pool = len({x.supplier_id for x in conv.final.reference_ranking})
    if synced:
        dec = buyer_decide(decide_items, params, rng, accepted_rules=accepted_rules, pool=pool,
                           not_permitted=not_permitted)
        by_dec = {d.ref: d for d in dec.items}
    else:
        dec = buyer_decide(decide_items, params, _NoDraw(), accepted_rules=accepted_rules, pool=pool,
                           not_permitted=not_permitted)
        by_dec = {d.ref: d for d in dec.items}
    first: dict[str, set[Action]] = {}                      # truth id -> allowed set of its first item
    for ref, tid, rule, _r in entries:
        if rule == "dup":
            allowed[ref] = set(first[tid])                       # the duplicate echoes the first item's answer
            continue
        if ref in allowed:                                   # decided before the buyer step (no level / fixed / out of domain)
            first[tid] = allowed[ref]
            continue
        d = by_dec[tid if rule is None else rule.ref]
        if not synced and d.reason in ("accepted", "declined"):
            rec = next((v for v in recorded.values() if v.get("constraint_id") == tid), None)
            u, p = (rec or {}).get("u"), (rec or {}).get("p_accept")
            allowed[ref] = ({Action.YES if float(u) < float(p) else Action.NO} if u is not None and p is not None
                            else {Action.YES, Action.NO})
        else:
            allowed[ref] = {d.action}
        first[tid] = allowed[ref]
    stays = synced
    if synced and _proof_enabled(settings) and any(
            d.action == Action.YES and d.ref in by_ref for d in dec.items):
        stays = False                                       # D055 draws after a YES: unknown count
    return allowed, stays


class _NoDraw:
    """rng stand-in for an unsynced replay: the probabilistic step is not evaluated (value never used)."""

    def random(self) -> float:
        return 0.0


def _by_belief(turn: Turn) -> dict:
    """D059: responses to proposal items keyed by ``belief_ref``; proof-request items (belief_ref None) are not
    proposal items and are skipped."""
    return {r.belief_ref: r for r in turn.item_responses if r.belief_ref is not None}


def policy_compliant(conv: GTConversation, prev: Turn, turn: Turn, settings: Optional[Mapping] = None, *,
                     rng=None, synced: bool = True) -> Optional[bool]:
    """Every item response of the buyer turn is in the replayed allowed set (see ``expected_responses``).
    Responses to proof requests (refs outside the proposal) are ignored.  ``rng`` defaults to a fresh
    ``buyer_rng(persona_seed)``, i.e. exact only for the conversation's first decision."""
    items = _proposal_items(prev)
    if not items or not turn.item_responses:
        return None
    if rng is None:
        rng = buyer_rng(conv.persona.persona_seed)
    allowed, _ = expected_responses(conv, prev, settings, rng, synced)
    resp = _by_belief(turn)
    for it in items:
        r = resp.get(it.constraint_ref)
        if r is None or r.action not in allowed.get(it.constraint_ref, {Action.NO}):
            return False
    return True


def policy_counts(conv: GTConversation, settings: Optional[Mapping] = None, *,
                  rng_factory: Callable[[object], object] = buyer_rng) -> tuple[int, int, int, int]:
    """(policy ok, policy evaluated, transition ok, transition evaluated) over buyer turns.  The rng is created once
    per conversation (``rng_factory(persona_seed)``) and advanced in turn order, exactly like the buyer."""
    pok = pev = tok = tev = 0
    turns = conv.turns
    rng = rng_factory(conv.persona.persona_seed)
    synced = True
    for i, t in enumerate(turns):
        if t.speaker != Speaker.BUYER or not t.item_responses:
            continue
        tev += 1
        tok += int(state_transition_ok(t))
        prev = next((p for p in reversed(turns[:i]) if p.speaker == Speaker.NEGOTIATOR), None)
        if prev is None or not _proposal_items(prev):
            continue
        allowed, synced = expected_responses(conv, prev, settings, rng, synced)
        resp = _by_belief(t)
        ok = all((resp.get(it.constraint_ref) is not None
                  and resp[it.constraint_ref].action in allowed.get(it.constraint_ref, {Action.NO}))
                 for it in _proposal_items(prev))
        pev += 1
        pok += int(ok)
    return pok, pev, tok, tev


def turns_to_agreement(conv: GTConversation) -> Optional[int]:
    if conv.final.outcome != "RECOMMENDED":
        return None
    for i, t in enumerate(conv.turns, start=1):
        if t.action == Action.RECOMMEND:
            return i
    return len(conv.turns)


def top1_has_verification_required(conv: GTConversation, shortlist_k: int) -> Optional[bool]:
    """Does the top-1 recommendation carry a non-empty verification_required list?  RECOMMENDED: first recommended
    row of the negotiator ranking; BEST_AVAILABLE: first item.  None if there is no top-1."""
    if conv.final.outcome == "BEST_AVAILABLE":
        items = conv.final.best_available.items if conv.final.best_available else []
        return bool(items[0].verification_required) if items else None
    rec = recommended_rows(conv, shortlist_k)
    return bool(rec[0].verification_required) if rec else None


def per_tier_summary(convs: Sequence[GTConversation]) -> dict[str, dict]:
    """Per persona.tier: n conversations, perfect-deal share (over those with top1_perfect_deal set) and the
    distribution of top1_match_original (n, mean, min, p25, median, p75, max)."""
    out: dict[str, dict] = {}
    for tier in Tier:
        cs = [c for c in convs if c.persona.tier == tier]
        pd_ = [c.top1_perfect_deal for c in cs if c.top1_perfect_deal is not None]
        mo = [c.top1_match_original for c in cs if c.top1_match_original is not None]
        out[tier.value] = {
            "n_conversations": len(cs),
            "n_perfect_deal_evaluated": len(pd_),
            "perfect_deal_share": M.rate(sum(1 for x in pd_ if x), len(pd_)),
            "top1_match_original": M.distribution(mo),
        }
    return out


def evaluate_run(convs: Sequence[GTConversation], settings: Optional[Mapping] = None,
                 shortlist_k: Optional[int] = None) -> dict:
    """Aggregate metrics (macro-average over conversations for ranking metrics)."""
    settings = settings or {}
    ks = (settings.get("eval", {}) or {}).get("k_values", [1, 3, 5, 10])
    nbins = (settings.get("eval", {}) or {}).get("ece_bins", 10)
    neg = settings.get("negotiation", {}) or {}
    k_short = shortlist_k or neg.get("min_shortlist", 3)
    bounds_tol = float(((settings.get("relaxation", {}) or {}).get("belief_bounds_tolerance", 0.0)))
    per: dict[str, list[float]] = {}
    probs: list[float] = []
    outs: list[bool] = []
    ff_n = ff_d = hv_n = hv_d = pok = pev = tok = tev = 0
    gains: list[int] = []
    t2a: list[int] = []
    n_best = 0
    reasons = {"no_feasible": 0, "patience_exhausted": 0, "nothing_verified": 0}
    n_wrap = 0
    n_vr = n_vr_den = 0
    all_opts: list = []
    bacc_ok = bacc_n = 0
    covered: set[str] = set()
    universe: set[str] = set()
    for c in convs:
        for k, v in ranking_metrics(c.final.negotiator_ranking, c.final.reference_ranking, ks).items():
            if v is not None:
                per.setdefault(k, []).append(v)
        p, o = calibration(c.final.negotiator_ranking, c.final.reference_ranking)
        probs += p
        outs += o
        a, b = false_feasibility(c)
        ff_n += a
        ff_d += b
        a, b = hard_violations(c, k_short)
        hv_n += a
        hv_d += b
        a, b, d, e = policy_counts(c, settings)
        pok += a
        pev += b
        tok += d
        tev += e
        gains += [o.candidates_gained for o in c.final.accepted_relaxations]
        all_opts += list(c.final.accepted_relaxations)
        if c.relaxation_analysis:
            ba = RM.belief_bounds_accuracy(c.relaxation_analysis, bounds_tol, ref_map=RM.conversation_ref_map(c))
            bacc_ok += ba["n_correct"]
            bacc_n += ba["n_rules"]
        t = turns_to_agreement(c)
        if t is not None:
            t2a.append(t)
        n_best += int(c.final.outcome == "BEST_AVAILABLE")
        if c.final.outcome == "BEST_AVAILABLE" and c.final.best_available_reason in reasons:
            reasons[c.final.best_available_reason] += 1
        n_wrap += int(any(t.action == Action.WRAP_UP for t in c.turns))
        vr = top1_has_verification_required(c, k_short)
        if vr is not None:
            n_vr_den += 1
            n_vr += int(vr)
        universe |= {r.product_id for r in c.final.reference_ranking}
        covered |= {r.product_id for r in recommended_rows(c, k_short)}
        if c.final.outcome == "BEST_AVAILABLE" and c.final.best_available:
            covered |= {it.product_id for it in c.final.best_available.items}
    mean = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
    out: dict = {k: mean(v) for k, v in sorted(per.items())}
    out.update({
        "n_conversations": len(convs),
        "brier": M.brier_score(probs, outs) if probs else None,
        "ece": M.expected_calibration_error(probs, outs, nbins) if probs else None,
        "false_feasibility_rate": M.rate(ff_n, ff_d),
        "hard_violation_rate_recommended": M.rate(hv_n, hv_d),
        "policy_compliance_rate": M.rate(pok, pev),
        "state_transition_accuracy": M.rate(tok, tev),
        "candidates_gained_per_accepted_relaxation": mean(gains),
        "turns_to_agreement": mean(t2a),
        "catalogue_coverage": M.rate(len(covered & universe), len(universe)),
        "best_available_share": M.rate(n_best, len(convs)),
        "best_available_reason_share": {k: M.rate(v, n_best) for k, v in reasons.items()},
        "wrap_up_share": M.rate(n_wrap, len(convs)),
        "verification_required_coverage": M.rate(n_vr, n_vr_den),
        "gain_estimate_error": RM.gain_estimate_error(all_opts),
        "belief_bounds_accuracy": M.rate(bacc_ok, bacc_n),
        "per_tier": per_tier_summary(convs),
        "persona_diversity": PD.analyse_conversations(convs, settings).summary(),
        "realism": RL.analyse(convs, None, settings).summary(),     # budget check needs a CatalogueView: SKIP here
    })
    return out


def evaluate_file(path: str | Path, settings: Optional[Mapping] = None) -> dict:
    return evaluate_run(load_conversations(path), settings)
