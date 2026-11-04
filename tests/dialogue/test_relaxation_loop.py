"""D053 / D055 / D056 on small hand-built scenarios: counter, reject, bypass, proof requests, policy, mirror of the
calibration simulator, belief loop.  No network; the buyer rng is scripted where a test pins a code path.

SCENARIO G2 (counter).  Hard power >= 5 kW (all pass), soft delivery <= 10 days (DELIVERY, levels 10 / 11.5 / 13),
soft warranty >= 24 months (QUALITY, levels 24 / 20.4 / 16.8).  Suppliers are distinct (one product each).
    P1 d10 w24 : feasible          P2 d13 w24 : needs delivery L2        P3 d13 w24 : needs delivery L2
    P4 d11 w21 : needs delivery L1 AND warranty L1                       P5 power 3 kW : HARD fail (dead)
Front over suppliers (min_shortlist 3, feasible {P1}):
    {delivery L2} recovers P2, P3 (+2) at cost 1.0   <- reference (cheapest, first on the staircase)
    {delivery L2, warranty L1} recovers P2, P3, P4 (+3) at cost 1.5
    ({delivery L1, warranty L1} recovers only P4 (+1) at cost 1.0: not strictly more than the cheaper point: dropped)
Strict buyer, DELIVERY T_c = 1, QUALITY T_c = 2, scripted rng 0.0:
    round 1: delivery L2 > T_c 1 and 0 < 1 < 2 -> COUNTER at level 1 = 10 + 3 * (0.15 / 0.30) = 11.5.  Bounds: max 1.
    round 2: delivery domain [0, 1]: P2, P3 are out of reach; the front is now {delivery L1, warranty L1} (+P4, cost 1.0),
             every item <= the counter level; both inside tolerance -> YES, YES (2 new rules <= max_relaxations 3).
    feasible {P1, P4} = 2 < 3 and nothing left -> BEST_AVAILABLE.
"""
from __future__ import annotations

import copy
import json

import pytest

from gtgen.contracts import (
    Action, AttributeAlignment, BelievedConstraint, BuyerProfile, CandidateConstraint, CandidateView, CatalogueView,
    Hardness, RelaxationBundle, RelaxationOption, ScenarioBrief, Speaker, Tier, TranscriptTurn, TranscriptView,
)
from gtgen.dialogue import BuyerSimulator, Negotiator, TruthState, assemble_conversation, run_dialogue
from gtgen.matching import (
    EvalContext, baseline_levels, build_rules, config_from_settings, params_from_persona, simulate_negotiation,
)

from dialogue.helpers import (
    BID, EchoRunners, FakeRunners, ScriptedRng, bc, belief, cc, make_persona, _load_settings, legacy_levels,
)

PROFILE = BuyerProfile(company_name="Kaveri Agro", industry="Irrigation", city="Pune", style="Brief.",
                       llm_call_id="p")


def _cand(pid, d, w, power="6", price=1000):
    rows = [cc(pid, "rated_power", power, "kW"), cc(pid, "lead_time_days", str(d), "days"),
            cc(pid, "warranty", str(w), "months")]
    rows = [r.model_copy(update={"supplier_id": f"S{pid}"}) for r in rows]
    return CandidateView(product_id=pid, supplier_id=f"S{pid}", product_name=f"Pump {pid}", category_name="Pumps",
                         price=price, price_formatted=f"Rs {price}", supplier_name=f"Supplier {pid}", city="Pune",
                         rating=4.0, rating_count=10, member_since="5 yrs", constraints=rows)


def g2_catalogue() -> CatalogueView:
    return CatalogueView(scenario_id="g2", candidates=[
        _cand("P1", 10, 24), _cand("P2", 13, 24), _cand("P3", 13, 24), _cand("P4", 11, 21), _cand("P5", 10, 24, "3")])


def g2_brief() -> ScenarioBrief:
    return ScenarioBrief(scenario_id="g2", bid_ref=BID, quantity=10, quantity_unit="units", budget_inr=100000.0,
                         constraints=[
        bc("C1", "power_kw", ">=", "5", hard=True, unit="kW", category="TECHNICAL"),
        bc("C2", "delivery_days", "<=", "10", unit="days", category="DELIVERY"),
        bc("C3", "warranty_months", ">=", "24", unit="months", category="QUALITY")])


G2_BELIEFS = [belief("B1", "power_kw", ">=", "5", hard=True, unit="kW"),
              belief("B2", "delivery_days", "<=", "10", unit="days"),
              belief("B3", "warranty_months", ">=", "24", unit="months")]
G2_ALIGN = [AttributeAlignment(bid_id=BID, buyer_attribute=a, candidate_attribute=c, confidence=1.0, llm_call_id="a")
            for a, c in (("power_kw", "rated_power"), ("delivery_days", "lead_time_days"),
                         ("warranty_months", "warranty"))]


def g2_persona(**kw):
    kw.setdefault("level_tolerance", {"DELIVERY": 1.0, "QUALITY": 2.0, "TECHNICAL": 0.0})
    return make_persona(0.8, Tier.STRICT, patience_rounds=4, **kw)


def settings_for(proof: bool = False, rate: float = 1.0, cap: int = 2) -> dict:
    st = legacy_levels(_load_settings())
    st["dialogue"]["progressive_reveal"]["enabled"] = False
    st["proof_requests"]["enabled"] = proof
    st["proof_requests"]["rate_by_tier"] = {k: rate for k in ("lenient", "medium", "medium_strict", "strict")}
    st["proof_requests"]["max_per_turn"] = cap
    return st


def run_g2(monkeypatch, st, *, rng=(0.0,), persona=None, runners=None, brief=None, catalogue=None, **kw):
    monkeypatch.setattr("gtgen.dialogue.buyer.buyer_rng", lambda seed: ScriptedRng(list(rng)))
    cat = catalogue or g2_catalogue()
    r = runners or EchoRunners(beliefs=G2_BELIEFS)
    ds = run_dialogue(brief or g2_brief(), persona or g2_persona(), PROFILE, cat, G2_ALIGN, r, st, **kw)
    return ds, assemble_conversation(ds, "run_g2", st), r


# ====================================================================== counter
def test_counter_sets_max_and_the_next_proposal_stays_at_or_below_it(monkeypatch):
    ds, conv, r = run_g2(monkeypatch, settings_for())
    assert [(t.speaker.value, t.action.value) for t in conv.turns] == [
        ("BUYER", "OPEN_REQUEST"), ("NEGOTIATOR", "PROPOSE_RELAXATION"), ("BUYER", "COUNTER"),
        ("NEGOTIATOR", "PROPOSE_RELAXATION"), ("BUYER", "YES"), ("NEGOTIATOR", "BEST_AVAILABLE")]
    first = conv.turns[1].proposal.bundle.items
    assert [(o.constraint_ref, o.from_value, o.to_value, o.candidates_gained) for o in first] == [
        ("B2", "10", "13", 2)]
    c = conv.turns[2]
    assert (c.proposal_outcome, c.counter_value) == ("COUNTERED", "11.5")
    assert [(x.constraint_ref, x.belief_ref, x.action, x.counter_value) for x in c.item_responses] == [
        ("C2", "B2", Action.COUNTER, "11.5")]
    rows = conv.relaxation_analysis
    assert rows[0].bounds_after_truth == {"C2": {"max": 1}} and rows[1].bounds_before_belief == {"B2": {"max": 1}}
    # next proposal: every item at or below the counter level (delivery level 1 = 11.5), plus warranty L1
    second = conv.turns[3].proposal.bundle.items
    assert [(o.constraint_ref, o.to_value) for o in second] == [("B2", "11.5"), ("B3", "20.4")]
    assert rows[1].chosen_point["levels"] == {"B2": 1, "B3": 1}
    assert all(lv <= 1 for lv in rows[1].chosen_point["levels"].values())
    # accepted: both fixed, front empty afterwards, BEST_AVAILABLE (feasible {P1, P4} = 2 < 3)
    assert rows[1].bounds_after_truth == {"C2": {"fixed": 1}, "C3": {"fixed": 1}}
    assert conv.final.outcome == "BEST_AVAILABLE" and conv.final.best_available_reason == "no_feasible"
    assert [(o.constraint_ref, o.to_value, o.truth_gain, o.negotiator_estimated_gain)
            for o in conv.final.accepted_relaxations] == [("B2", "11.5", 0, 0), ("B3", "20.4", 1, 1)]
    assert len(rows) >= 3 and [w.chosen_action for w in rows] == ["PROPOSE_RELAXATION"] * 2 + ["BEST_AVAILABLE"]


def test_counter_value_is_a_level_the_negotiator_can_read(monkeypatch):
    """The counter text is on the buyer's own level grid: 11.5 = 10 + 0.15 * 10 -> level 1 on the negotiator's side."""
    ds, conv, r = run_g2(monkeypatch, settings_for())
    assert conv.relaxation_analysis[0].buyer_decision["B2"] == {
        "constraint_id": "C2", "action": "COUNTER", "proposed_level": 2, "level": 1, "reason": "tolerance",
        "counter_value": "11.5"}


@pytest.mark.parametrize("tier", [Tier.LENIENT, Tier.MEDIUM])
def test_lenient_and_medium_also_counter(monkeypatch, tier):
    """D082: every tier counters a level-2 delivery ask at the level-1 tolerance (11.5), no draw."""
    pers = make_persona(0.1 if tier == Tier.LENIENT else 0.4, tier, level_tolerance={"DELIVERY": 1.0, "QUALITY": 2.0,
                                                                                  "TECHNICAL": 0.0})
    ds, conv, r = run_g2(monkeypatch, settings_for(), persona=pers)
    assert any(t.action == Action.COUNTER for t in conv.turns)
    assert conv.relaxation_analysis[0].buyer_decision["B2"]["action"] == "COUNTER"
    assert conv.relaxation_analysis[0].buyer_decision["B2"]["counter_value"] == "11.5"
    assert conv.relaxation_analysis[0].bounds_after_truth == {"C2": {"max": 1}}


# ====================================================================== reject / hard
def test_hard_relaxation_never_gets_yes_even_when_the_negotiator_believes_it_is_soft(monkeypatch):
    """The negotiator believes the power is SOFT (it only heard 'ideally'); the truth is HARD -> NO, never YES."""
    cat = CatalogueView(scenario_id="g2", candidates=[
        _cand("P1", 10, 24, "5.5"), _cand("P2", 10, 24, "4.6"), _cand("P3", 10, 24, "4.6"), _cand("P4", 10, 24, "3")])
    soft_power = [belief("B1", "power_kw", ">=", "5", unit="kW")] + G2_BELIEFS[1:]
    ds, conv, r = run_g2(monkeypatch, settings_for(), catalogue=cat, runners=EchoRunners(beliefs=soft_power))
    first = conv.relaxation_analysis[0]
    assert first.chosen_point["levels"] == {"B1": 1}                    # 5 -> 4.25 would recover P2, P3 (4.6)
    assert first.buyer_decision["B1"]["action"] == "NO" and first.buyer_decision["B1"]["reason"] == "policy"
    assert all(a.constraint_ref != "B1" for a in conv.final.accepted_relaxations)
    assert first.bounds_after_truth == {}                               # a HARD rule gets no bound at all (not a rule)
    assert first.bounds_after_belief == {"B1": {"max": 0}}          # the negotiator read the refusal from the text
    assert all(not any("B1" in p["levels"] for p in w.front_points) for w in conv.relaxation_analysis[2:])


# ====================================================================== proof requests (D055)
def test_proof_request_is_a_bypass_that_changes_no_ranking_input(monkeypatch):
    base_ds, base, _ = run_g2(monkeypatch, settings_for(proof=False))
    ds, conv, r = run_g2(monkeypatch, settings_for(proof=True, rate=1.0, cap=1))
    # the buyer asked for proof of the first YES item (sorted by constraint id: C2 delivery), cap 1
    pr = conv.final.proof_requests
    assert len(pr) == 1 and (pr[0].constraint_id, pr[0].attribute, pr[0].doc_name) == (
        "C2", "delivery_days", "delivery days test report")
    assert pr[0].turn_idx == 4 and pr[0].product_id == "P4"            # the candidate the relaxation brought in
    t = conv.turns[4]
    assert t.proof_requests == pr and t.action == Action.YES and "Please send the delivery days test report." in t.utterance
    assert t.item_responses[-1].action == Action.BYPASS and t.item_responses[-1].constraint_ref == "C2" \
        and t.item_responses[-1].belief_ref is None
    assert t.proposal_outcome == "ACCEPTED"                              # the proposal items keep their own answers
    # the negotiator answers with ACKNOWLEDGE, naming the document, from catalogue evidence
    ack = conv.turns[5]
    assert ack.speaker == Speaker.NEGOTIATOR and ack.action == Action.ACKNOWLEDGE
    assert "delivery days test report" in ack.utterance
    (views, evidence), = r.ack_calls
    assert [(v.attribute, v.doc_name) for v in views] == [("delivery days", "delivery days test report")]
    assert evidence[0].evidence_id == views[0].evidence_id and "Pump" in evidence[0].text
    # the doc is in the candidate's list and the conversation list ...
    ref = {x.product_id: x for x in conv.final.reference_ranking}
    assert ref["P4"].pending_verification_docs == ["delivery days test report"]
    assert conv.final.pending_verification_docs == ["delivery days test report"]
    # ... and NOTHING else changed: ranking rows byte-equal except pending_verification_docs
    def strip(rows):
        return [r.model_copy(update={"pending_verification_docs": []}).model_dump_json() for r in rows]
    assert strip(conv.final.reference_ranking) == strip(base.final.reference_ranking)
    assert strip(conv.final.reference_ranking_initial) == strip(base.final.reference_ranking_initial)
    assert strip(conv.final.negotiator_ranking) == strip(base.final.negotiator_ranking)
    assert [c.model_dump_json() for c in conv.final.compliance_detail_truth] == [
        c.model_dump_json() for c in base.final.compliance_detail_truth]               # cell statuses unchanged
    assert (conv.top1_match_original, conv.top1_perfect_deal, conv.final.outcome) == (
        base.top1_match_original, base.top1_perfect_deal, base.final.outcome)
    # front and bounds unchanged: the same relaxation rows apart from the extra ACKNOWLEDGE row
    keep = [w for w in conv.relaxation_analysis if w.chosen_action != "ACKNOWLEDGE"]
    assert [(w.front_points, w.bounds_after_truth, w.verified_after_truth) for w in keep] == [
        (w.front_points, w.bounds_after_truth, w.verified_after_truth) for w in base.relaxation_analysis]
    assert [x.model_dump_json() for x in conv.final.accepted_relaxations] == [
        x.model_dump_json() for x in base.final.accepted_relaxations]


def test_proof_request_is_a_seeded_draw_from_the_buyer_rng_with_a_cap(monkeypatch):
    # cap 2, rate 1.0, rng 0.0: both YES items get a request, in constraint-id order
    ds, conv, r = run_g2(monkeypatch, settings_for(proof=True, rate=1.0, cap=2))
    assert [(p.constraint_id, p.doc_name) for p in conv.final.proof_requests] == [
        ("C2", "delivery days test report"), ("C3", "warranty months test report")]
    # rate 0: no request even with the feature on
    ds, conv, r = run_g2(monkeypatch, settings_for(proof=True, rate=0.0, cap=2))
    assert conv.final.proof_requests == [] and all(t.action != Action.ACKNOWLEDGE for t in conv.turns)
    # the draw consumes the SAME stream as the decisions: rng values 0.0 (decision C2), 0.0 (decision C3), then 0.9
    # (proof draw for C2 fails the rate 0.5) and 0.1 (C3 passes)
    ds, conv, r = run_g2(monkeypatch, settings_for(proof=True, rate=0.5, cap=2), rng=(0.0, 0.0, 0.9, 0.1))
    assert [p.constraint_id for p in conv.final.proof_requests] == ["C3"]


def test_proof_request_in_the_buyer_payload_and_ack_barrier(monkeypatch):
    ds, conv, r = run_g2(monkeypatch, settings_for(proof=True, rate=1.0, cap=1))
    (args, kwargs), = [(a, k) for a, k in [(a, k) for n, a, k in r.calls if n == "buyer_reply"][-1:]]
    items = args[3]
    assert [i.action for i in items] == ["YES", "YES", "PROOF_REQUEST"] and items[-1].doc_name == "delivery days test report"
    assert kwargs["persona"].tier == Tier.STRICT and kwargs["ctx"].turn_idx == 4
    assert kwargs["already_stated"] == []                               # progressive reveal OFF: nothing tracked here
    for n, a, k in r.calls:
        if n in ("negotiator_acknowledge", "negotiator_reply", "negotiator_interpret"):
            blob = json.dumps([a, k], default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o))
            for bad in ("golden_param", '"matched_constraint_id": "', "proof_request", "C2", "C3", '"fixed"', '"max"'):
                assert bad not in blob, (n, bad)


def test_bypass_on_a_doc_proof_item_keeps_front_and_bounds(monkeypatch):
    """A proposal item the buyer treats as doc-proof is answered BYPASS: verification list only, no bound, no change."""
    cons = [bc("C1", "power_kw", ">=", "5", hard=True, unit="kW", category="TECHNICAL"),
            bc("C2", "delivery_days", "<=", "10", unit="days", category="DELIVERY"),
            bc("C9", "iso_certification", "=", "ISO 9001", doc=True, doc_name="ISO 9001 certificate",
               category="QUALITY")]
    st = settings_for()
    brief = ScenarioBrief(scenario_id="g2", bid_ref=BID, quantity=10, quantity_unit="units", constraints=cons)
    truth = TruthState(cons, g2_catalogue().candidates, EvalContext(), config_from_settings(st))
    assert "C9" not in truth.by_ref                                       # a doc-proof constraint is not a rule
    buyer = BuyerSimulator(brief, g2_persona(), PROFILE, EchoRunners(), st, truth=truth,
                           rng=ScriptedRng([0.0]))
    before = (truth.bounds, list(truth.feasible), truth.levels)
    bundle = RelaxationBundle(items=[RelaxationOption(
        constraint_ref="BX", kind="numeric_widen", from_value="ISO 9001", to_value="ISO 9001",
        normalised_cost=0.5, candidates_gained=1, feasible_after=2)])
    tc = cons[2]
    resp = buyer.respond_to_proposal(bundle, {"BX": tc}, TranscriptView(scenario_id="g2", turns=[]), seed=1,
                                     turn_idx=2)
    assert resp.action == Action.BYPASS and resp.outcome == "BYPASSED"
    assert [r.action for r in resp.responses] == [Action.BYPASS]
    assert resp.accepted == [] and resp.decision["BX"]["reason"] == "verification"
    assert (truth.bounds, list(truth.feasible), truth.levels) == before
    assert truth.state.bypassed == ("C9",)


# ====================================================================== buyer policy (unit)
def _unit_buyer(persona, rng=(0.0,), settings=None):
    st = settings or settings_for()
    cons = g2_brief().constraints
    truth = TruthState(cons, g2_catalogue().candidates, EvalContext(), config_from_settings(st))
    brief = g2_brief()
    return BuyerSimulator(brief, persona, PROFILE, EchoRunners(), st, truth=truth, rng=ScriptedRng(list(rng))), truth, cons


def _opt(ref, frm, to):
    return RelaxationOption(constraint_ref=ref, kind="numeric_widen", from_value=frm, to_value=to,
                            normalised_cost=0.5, candidates_gained=1, feasible_after=2)


def _respond(buyer, items, resolved, turn=2):
    return buyer.respond_to_proposal(RelaxationBundle(items=items), resolved,
                                     TranscriptView(scenario_id="g2", turns=[]), seed=1, turn_idx=turn)


def test_unit_hard_constraint_is_no_and_unmatched_is_yes_without_state_change():
    buyer, truth, cons = _unit_buyer(g2_persona())
    r = _respond(buyer, [_opt("BA", "5", "4.25"), _opt("BZ", "7", "8")], {"BA": cons[0], "BZ": None})
    assert [(x.constraint_ref, x.belief_ref, x.action) for x in r.responses] == [
        ("C1", "BA", Action.NO), ("unmatched:BZ", "BZ", Action.YES)]            # unmatched belief: no truth id exists (flagged)
    assert r.decision["BA"]["reason"] == "policy" and r.decision["BZ"]["reason"] == "unmatched"
    assert truth.bounds == {} and truth.state.changes == ()
    assert [(o.constraint_ref, o.truth_gain) for o in r.accepted] == [("BZ", 0)]
    assert "unmatched_belief:BZ" in buyer.drain_qa_flags()


def test_unit_limit_tolerance_bounded_already_and_out_of_range():
    buyer, truth, cons = _unit_buyer(g2_persona(max_relaxations=1))
    r = _respond(buyer, [_opt("B2", "10", "11.5"), _opt("B3", "24", "20.4")], {"B2": cons[1], "B3": cons[2]})
    assert [(x.constraint_ref, x.belief_ref, x.action) for x in r.responses] == [
        ("C2", "B2", Action.YES), ("C3", "B3", Action.NO)]
    assert r.decision["B3"]["reason"] == "limit" and truth.bounds == {"C2": {"fixed": 1}, "C3": {"max": 0}}
    # same value again: already satisfied -> YES, no draw, no new change; refused rule again -> NO 'bounded'
    n_changes = len(truth.state.changes)
    rng_before = buyer.rng.n
    r = _respond(buyer, [_opt("B2", "10", "11.5"), _opt("B3", "24", "20.4"), _opt("B4", "10", "40")],
                 {"B2": cons[1], "B3": cons[2], "B4": cons[1]}, turn=4)
    assert [x.action for x in r.responses] == [Action.YES, Action.NO, Action.YES]        # B4 duplicates B2's rule
    assert [r.decision[k]["reason"] for k in ("B2", "B3")] == ["already", "bounded"]
    assert len(truth.state.changes) == n_changes and buyer.rng.n == rng_before
    # a value beyond the top level cannot be mapped to a level: NO 'out_of_range', still no draw
    buyer2, truth2, cons2 = _unit_buyer(g2_persona())
    r = _respond(buyer2, [_opt("B2", "10", "20")], {"B2": cons2[1]})
    assert r.decision["B2"]["reason"] == "out_of_range" and r.responses[0].action == Action.NO and buyer2.rng.n == 0


def test_unit_multi_item_proposal_counters_every_item_at_relaxation_limit():
    persona = g2_persona(max_relaxations=1,
                         level_tolerance={"DELIVERY": 1.0, "QUALITY": 1.0, "TECHNICAL": 0.0})
    buyer, truth, cons = _unit_buyer(persona)
    r = _respond(buyer, [_opt("B2", "10", "13"), _opt("B3", "24", "16.8")], {"B2": cons[1], "B3": cons[2]})
    assert [(x.constraint_ref, x.belief_ref, x.action, x.counter_value) for x in r.responses] == [
        ("C2", "B2", Action.COUNTER, "11.5"), ("C3", "B3", Action.COUNTER, "20.4")]
    assert truth.bounds == {"C2": {"max": 1}, "C3": {"max": 1}}


def test_unit_strict_counters_at_tolerance_and_yes_needs_the_draw():
    buyer, truth, cons = _unit_buyer(g2_persona(), rng=(0.99,))
    r = _respond(buyer, [_opt("B2", "10", "13")], {"B2": cons[1]})              # level 2 > T_c 1 -> COUNTER, no draw
    assert r.responses[0].action == Action.COUNTER and r.responses[0].counter_value == "11.5" and buyer.rng.n == 0
    assert truth.bounds == {"C2": {"max": 1}}
    r = _respond(buyer, [_opt("B2", "10", "11.5")], {"B2": cons[1]}, turn=4)    # level 1 within tolerance: draw 0.99 > p
    assert r.responses[0].action == Action.NO and r.decision["B2"]["reason"] == "declined" and buyer.rng.n == 1
    assert truth.bounds == {"C2": {"max": 0}}


# ====================================================================== mirror of the calibration simulator
@pytest.mark.parametrize("seed,tau,tier,tol", [
    (1, 0.1, Tier.LENIENT, {"DELIVERY": 2.0, "QUALITY": 2.0}),
    (2, 0.3, Tier.MEDIUM, {"DELIVERY": 1.0, "QUALITY": 2.0}),
    (5, 0.6, Tier.MEDIUM_STRICT, {"DELIVERY": 2.0, "QUALITY": 1.0}),
    (9, 0.9, Tier.STRICT, {"DELIVERY": 1.0, "QUALITY": 2.0}),
])
def test_dialogue_reaches_the_same_truth_state_as_simulate_negotiation(seed, tau, tier, tol):
    """The calibration simulator and the dialogue run the same loop: with faithful text and no proof requests the
    final truth bounds and levels are identical (same seed -> same buyer stream)."""
    st = settings_for()
    pers = make_persona(tau, tier, seed=seed, level_tolerance={**tol, "TECHNICAL": 0.0}, max_relaxations=3,
                        patience_rounds=3)
    cat, br = g2_catalogue(), g2_brief()
    ds = run_dialogue(br, pers, PROFILE, cat, G2_ALIGN, EchoRunners(beliefs=G2_BELIEFS), st)
    cfg = config_from_settings(st)
    ctx = EvalContext(alignments={a.buyer_attribute: a for a in G2_ALIGN}, quantity=br.quantity,
                      verification_only_fail_blocks=st["scenario"]["verification_only_fail_blocks"],
                      unverified_factor=st["ranking"]["unverified_text_factor"],
                      rel_tol=st["negotiation"]["bundle_search"]["numeric_eq_rel_tolerance"])
    sim = simulate_negotiation(br.constraints, cat.candidates, ctx, cfg, params_from_persona(pers, cfg),
                               seed=seed, min_shortlist=st["negotiation"]["min_shortlist"])
    assert {k: dict(v) for k, v in sim.state.bounds.items()} == ds.truth_bounds
    rules = build_rules(br.constraints, cfg)
    assert dict(sim.levels) == baseline_levels(rules, ds.truth_bounds)
    assert len(sim.state.changes) == sum(1 for b in ds.truth_bounds.values() if "fixed" in b)
    proposals = [w for w in ds.relaxation_analysis if w.chosen_action == "PROPOSE_RELAXATION"]
    assert len(sim.rows) == len(proposals)


# ====================================================================== RELAXATION_LOGIC.md 8.6, dialogue side
def test_spec_8_6_worked_example_through_the_dialogue(monkeypatch):
    """The matching module pins the front itself (tests/matching/test_front.py); this is the dialogue side of the same world.
    Rules A (alpha >= 10) and B (beta >= 10), levels [0, 1/2] (value 10 -> 5), gtgen reading: both SOFT.
        P1 (5, 5) needs A1 and B1 | P2 (5, 10) needs A1 | P3 (10, 5) needs B1 | no product is feasible at the start.
    Front: (A0, B1) -> P3 at cost 1, then (A1, B1) -> P1, P2, P3 at cost 2 ((A1, B0) is dropped: it only recovers P2).
    The negotiator proposes the cheapest point, re-derives the front from the fixed value after the YES, proposes the
    remaining step, and recommends once min_shortlist 3 is met.  Relaxing one rule at a time would never recover P1."""
    st = settings_for()
    st["relaxation"] = {**st["relaxation"], "levels": [0.0, 0.5], "default_max_deviation": 0.5}
    cons = [bc("A", "alpha", ">=", "10", category="GENERAL"), bc("B", "beta", ">=", "10", category="GENERAL")]
    brief = ScenarioBrief(scenario_id="s86", bid_ref=BID, quantity=1, quantity_unit="units", budget_inr=1000.0,
                          constraints=cons)

    def cand(pid, a, b):
        rows = [cc(pid, "alpha", str(a)), cc(pid, "beta", str(b))]
        rows = [r.model_copy(update={"supplier_id": f"S{pid}"}) for r in rows]
        return CandidateView(product_id=pid, supplier_id=f"S{pid}", product_name=pid, category_name="W", price=100,
                             price_formatted="Rs 100", supplier_name=f"Supplier {pid}", city="Pune", rating=4.0,
                             rating_count=10, member_since="5 yrs", constraints=rows)
    cat = CatalogueView(scenario_id="s86", candidates=[cand("P1", 5, 5), cand("P2", 5, 10), cand("P3", 10, 5)])
    align = [AttributeAlignment(bid_id=BID, buyer_attribute=a, candidate_attribute=a, confidence=1.0, llm_call_id="a")
             for a in ("alpha", "beta")]
    beliefs = [belief("B1", "alpha", ">=", "10"), belief("B2", "beta", ">=", "10")]
    monkeypatch.setattr("gtgen.dialogue.buyer.buyer_rng", lambda seed: ScriptedRng([0.0]))
    pers = make_persona(0.1, Tier.LENIENT, level_tolerance={"GENERAL": 1.0}, max_relaxations=3, patience_rounds=4)
    ds = run_dialogue(brief, pers, PROFILE, cat, align, EchoRunners(beliefs=beliefs), st)
    conv = assemble_conversation(ds, "run_s86", st)
    rows = conv.relaxation_analysis
    assert [w.chosen_action for w in rows] == ["PROPOSE_RELAXATION", "PROPOSE_RELAXATION", "RECOMMEND"]
    # step 1: the staircase of the spec (cost 1, then cost 2) and the cheapest point: beta 10 -> 5 recovers P3
    assert [(p["levels"], p["cost"], p["supplier_count"]) for p in rows[0].front_points] == [
        ({"B2": 1}, 1.0, 1), ({"B1": 1, "B2": 1}, 2.0, 3)]
    first = conv.turns[1].proposal.bundle
    assert [(o.constraint_ref, o.from_value, o.to_value, o.gained_product_ids) for o in first.items] == [
        ("B2", "10", "5", ["P3"])]
    assert (rows[0].verified_before_truth, rows[0].verified_after_truth) == (0, 1)
    # step 2: B is fixed at level 1, the front is recomputed from it: only A is left, and it recovers P1 and P2 together
    assert rows[1].bounds_before_belief == {"B2": {"fixed": 1}}
    assert [(p["levels"], p["cost"], p["supplier_count"]) for p in rows[1].front_points] == [({"B1": 1}, 2.0, 2)]      # recovered beyond P3
    second = conv.turns[3].proposal.bundle
    assert [(o.constraint_ref, o.from_value, o.to_value, o.gained_product_ids) for o in second.items] == [
        ("B1", "10", "5", ["P1", "P2"])]
    assert (rows[1].verified_before_truth, rows[1].verified_after_truth) == (1, 3)
    # the end: three verified suppliers -> RECOMMEND, all three on the shortlist, truth and belief agree
    assert conv.final.outcome == "RECOMMENDED"
    assert {x.product_id for x in conv.final.reference_ranking if x.p_feasible == 1.0} == {"P1", "P2", "P3"}
    assert {x.product_id for x in conv.final.negotiator_ranking if x.p_feasible == 1.0} == {"P1", "P2", "P3"}
    assert [o.truth_gain for o in conv.final.accepted_relaxations] == [1, 2]
    # without the joint step P1 stays out: a single-rule proposal for A alone recovers P2 only
    only_a = {"B1": 1}
    assert only_a not in [p["levels"] for p in rows[0].front_points]
