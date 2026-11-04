"""Small hand-written synthetic fixtures for the dialogue-time prompts (tests only; no live calls).

``python -m gtgen.llm.synth_fixtures`` writes tests/fixtures/llm/<prompt>/<hash>.json by running each case
through a client in record mode whose HTTP layer is replaced by the canned response below.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from gtgen.contracts import (
    BelievedConstraint, Persona, Tier, BuyerConstraint, BuyerProfile, Hardness, RelaxationBundle, RelaxationOption, Speaker,
    TranscriptTurn, TranscriptView,
)

from .context import ConversationContext
from .runners import DocClaimRequest
from .schemas import BuyerItemDecision, EvidenceSnippet, MenuItemView, MenuPointView, ProofRequestView, RequirementStatement

PROFILE = BuyerProfile(company_name="Kaveri Agro Pumps Pvt Ltd", industry="Agricultural irrigation",
                       city="Coimbatore", style="Warm but exacting; short sentences.", llm_call_id="synthetic")
OPENING_TEXT = ("Hello, this is Kaveri Agro Pumps in Coimbatore. We need 3 submersible pump sets. The motor rating "
                "must be 1.5 HP, and ideally delivery within 15 days.")
TRANSCRIPT = TranscriptView(scenario_id="syn", turns=[
    TranscriptTurn(turn_idx=0, speaker=Speaker.BUYER, utterance=OPENING_TEXT)])
OPENING_FORMATTED_TEXT = ("Hello, this is Kaveri Agro Pumps in Coimbatore. We need 500 submersible pump sets for a "
                          "total of INR 2,15,336. The motor rating must be 1.5 HP.")
BELIEFS = [
    BelievedConstraint(belief_id="B1", attribute="motor_rating", operator="=", value="1.5", unit="HP",
                       believed_hardness=Hardness.HARD, source_turn=0),
    BelievedConstraint(belief_id="B2", attribute="delivery_days", operator="<=", value="15", unit="days",
                       believed_hardness=Hardness.SOFT, source_turn=0),
]
BUNDLE = RelaxationBundle(
    items=[RelaxationOption(constraint_ref="B2", kind="numeric_widen", from_value="15", to_value="25",
                            normalised_cost=0.4, candidates_gained=2, feasible_after=5)],
    joint_gain=2, feasible_after=5, total_normalised_cost=0.4)
EVIDENCE = [
    EvidenceSnippet(evidence_id="P1", text="Aqua Pumps (Rajkot): 1.5 HP submersible pump, rating 4.2, delivery in 25 days."),
    EvidenceSnippet(evidence_id="P2", text="Bharat Motors (Pune): 1.5 HP pump, ISI marked, rating 4.0."),
]
BRIEF_CONSTRAINTS = [
    BuyerConstraint(constraint_id="C1", bid_id="b", golden_param=True, hardness=Hardness.HARD,
                    attribute="motor_rating", commercial_text="Motor rating 1.5 HP."),
    BuyerConstraint(constraint_id="C2", bid_id="b", golden_param=False, hardness=Hardness.SOFT,
                    attribute="delivery_days", commercial_text="Delivery within 15 days."),
    BuyerConstraint(constraint_id="C3", bid_id="b", golden_param=True, hardness=Hardness.HARD,
                    attribute="bee_rating", commercial_text="BEE rating 3 star."),
]
REPLY_ITEMS = [
    BuyerItemDecision(item="Delivery within 15 days", action="COUNTER", strength="prefer",
                      proposed_value="25 days", counter_value="20 days"),
    BuyerItemDecision(item="Motor rating 1.5 HP", action="NO", strength="must", proposed_value="1.0 HP"),
]
REPLY_TEXT = "Delivery in 25 days is a stretch for us; 20 days I can live with. The 1.5 HP motor rating is not something we can change."
NEG_REPLY_TEXT = ("Thanks for the details. Could you stretch delivery from 15 to 25 days? That would open up 2 more "
                  "options, for example Aqua Pumps (Rajkot) with a 1.5 HP submersible pump.")
CLAIM_TEXT = "Aqua Pumps in Rajkot has a 1.5 HP pump rated 4.2, and it comes with a 5 year warranty."

CTX = ConversationContext(scenario_id="syn-1", cache_seed=11)
SALES_PERSONA = {"speaking_style": "Warm, quick to smile, plain business English with a light Chennai touch.",
                 "sales_approach": "Eager to close; explains trade-offs openly and never promises beyond the listing."}
# D078: no name any more - the negotiator never introduces itself by name, so the line has no leading "Name:".
SALES_PERSONA_LINE = "warm, quick to smile, plain business English; eager to close but never promises beyond the listing."
COMMERCIAL_TEXT = ("We have a budget of INR 3,45,000 in mind, and we would prefer a supplier from Gujarat or "
                   "Maharashtra.")
TRANSCRIPT_COMMERCIAL = TranscriptView(scenario_id="syn", turns=[
    TranscriptTurn(turn_idx=0, speaker=Speaker.BUYER, utterance=COMMERCIAL_TEXT)])
FINAL_FACTS = {
    "options": [
        {"rank": 1, "product": "1.5 HP submersible pump", "supplier": "Aqua Pumps", "price_text": "INR 28,500 per unit",
         "met": ["motor rating", "delivery in 15 days"], "unmet": [], "to_confirm": ["ISO 9001 certificate"]},
        {"rank": 2, "product": "1.5 HP pump set", "supplier": "Bharat Motors", "price_text": None,
         "met": ["motor rating"], "unmet": [], "to_confirm": []}],
    "to_confirm": ["ISO 9001 certificate from Aqua Pumps"], "note": None}
FINAL_RECOMMEND_TEXT = ("Good news: Aqua Pumps looks like the best fit with its 1.5 HP submersible pump at INR 28,500 per "
                        "unit, and delivery in 15 days. The only open point is the ISO 9001 certificate, which the "
                        "supplier still has to show us. Bharat Motors is a solid second choice with the right motor "
                        "rating. Aqua Pumps is the recommended option; still open with the supplier: the ISO 9001 certificate.")
FINAL_FACTS_BEST = {
    "options": [
        {"rank": 1, "product": "1.5 HP submersible pump", "supplier": "Aqua Pumps", "price_text": "INR 28,500 per unit",
         "met": ["motor rating"], "unmet": ["delivery within 15 days"], "to_confirm": []},
        {"rank": 2, "product": "1.5 HP pump set", "supplier": "Bharat Motors", "price_text": None,
         "met": ["motor rating"], "unmet": [], "to_confirm": ["BIS licence"]}],
    "to_confirm": [], "note": "no listing meets every requirement"}
FINAL_BEST_TEXT = ("Nothing on the market ticks every box today, but Aqua Pumps comes closest: its 1.5 HP submersible pump "
                   "at INR 28,500 per unit has the right motor, though delivery would run longer than 15 days. "
                   "Bharat Motors also has the right motor, and its BIS licence is still to be confirmed. "
                   "The longer delivery is the one compromise with Aqua Pumps; still open with the suppliers: the BIS licence.")
PERSONA_OBJ = Persona(
    persona_seed=11, tau=0.642, tier=Tier.MEDIUM_STRICT, category_tolerance={"PRODUCT": 0.6}, counter_band=0.12,
    patience_turns=6, max_concessions_per_turn=2, ranking_weights={"spec_match": 1.0}, llm_call_id="synthetic")
MENU_POINT = MenuPointView(
    items=[MenuItemView(attribute="delivery_days", kind="numeric_widen", from_value="15", to_value="25")],
    extra_options=2)
ACK_EVIDENCE = [EvidenceSnippet(evidence_id="P1", text="Aqua Pumps (Rajkot): 1.5 HP submersible pump. ISO 9001:2015 certified company, rating 4.2.")]
PROOF_ITEMS = [ProofRequestView(attribute="quality_certification", doc_name="ISO 9001 certificate", evidence_id="P1")]
PROOF_BUYER_ITEMS = [BuyerItemDecision(item="Quality certification", action="PROOF_REQUEST", strength="prefer",
                                       doc_name="ISO 9001 certificate", claimed="Aqua Pumps is ISO 9001:2015 certified")]
PROOF_BUYER_TEXT = ("Good to hear Aqua Pumps is ISO 9001:2015 certified. Please send us the ISO 9001 certificate "
                    "before we commit.")
ACK_TEXT = ("Understood, you want proof before committing. For Aqua Pumps in Rajkot, listed as ISO 9001:2015 "
            "certified, the supplier will need to provide the ISO 9001 certificate.")
TOPICS = ["warranty period", "ISI certification", "delivery days"]
CLARIFY_TEXT = ("Thanks, that is a clear start. Do you need a particular warranty period, and is ISI certification "
                "important for you?")
REVEAL_REQS = [RequirementStatement(text="BEE rating 3 star.", strength="must")]
REVEAL_QUESTION = "Do you need a particular warranty period, and is ISI certification important for you?"
REVEAL_TEXT = "Yes, one more thing: we must have a BEE rating of 3 star."
PROPOSAL_ITEMS = [{"index": 0, "attribute": "delivery_days", "kind": "numeric_widen", "from_value": "15",
                   "to_value": "25"}]
INTERP_TRANSCRIPT = TranscriptView(scenario_id="syn", turns=[
    TranscriptTurn(turn_idx=0, speaker=Speaker.BUYER, utterance=OPENING_TEXT),
    TranscriptTurn(turn_idx=1, speaker=Speaker.NEGOTIATOR, utterance=NEG_REPLY_TEXT),
    TranscriptTurn(turn_idx=2, speaker=Speaker.BUYER, utterance=REPLY_TEXT)])

TRANSCRIPT_NEG = TranscriptView(scenario_id="syn", turns=[
    TranscriptTurn(turn_idx=0, speaker=Speaker.BUYER, utterance=OPENING_TEXT),
    TranscriptTurn(turn_idx=1, speaker=Speaker.NEGOTIATOR, utterance=NEG_REPLY_TEXT)])
TRANSCRIPT_ACK = TranscriptView(scenario_id="syn", turns=[
    TranscriptTurn(turn_idx=3, speaker=Speaker.NEGOTIATOR,
                   utterance="Aqua Pumps in Rajkot is listed as ISO 9001:2015 certified and has a 1.5 HP pump.")])
TRANSCRIPT_PROOF = TranscriptView(scenario_id="syn", turns=[
    TranscriptTurn(turn_idx=3, speaker=Speaker.NEGOTIATOR,
                   utterance="Aqua Pumps in Rajkot is listed as ISO 9001:2015 certified and has a 1.5 HP pump."),
    TranscriptTurn(turn_idx=4, speaker=Speaker.BUYER, utterance=PROOF_BUYER_TEXT)])
BANDS = {"terms": {
    "budget_inr": {"kind": "numeric", "lo": 150000.0, "hi": 400000.0, "unit": "INR", "tight_end": "lo"},
    "qty_vs_moq": {"kind": "numeric", "lo": 0.0, "hi": 1.0, "unit": "fraction", "tight_end": "lo"},
    "preferred_states": {"kind": "set", "options": ["Gujarat", "Maharashtra", "Tamil Nadu"], "min_k": 1,
                         "max_k": 2, "tight_end": "fewer"}},
    "omitted": {}}
PERSONA_V3_OUT = {
    "company_name": "Sahyadri Fluid Systems Pvt Ltd", "industry": "Textile dyeing and processing",
    "city": "Surat", "style": "Brisk and numbers-first; politely pushes back on every price.", "tau": 0.642,
    "category_tolerance": [{"category": "PRODUCT", "multiplier": 0.6}, {"category": "QUALITY", "multiplier": 0.9},
                           {"category": "LOCATION", "multiplier": 0.4}],
    "counter_band": 0.12, "patience_turns": 6, "max_concessions_per_turn": 2,
    "ranking_weights": {"spec_match": 0.3, "price_score": 0.25, "moq_fit": 0.1, "rating_bayes": 0.1,
                        "tenure_years": 0.05, "location_match": 0.1, "evidence_confidence": 0.1},
    "commercial_terms": {"budget_inr": 210000.0, "qty_vs_moq": 0.25, "preferred_states": ["Gujarat"]},
    # deliberately outside relaxation.persona_ranges: the runner must clamp (3 / 4 / 0.5 / 5.0)
    "max_relaxations": 5, "patience_rounds": 6, "decision_randomness": 0.8,
    "tolerance": [{"category": "PRODUCT", "value": 7.5}, {"category": "QUALITY", "value": 2.0},
                  {"category": "LOCATION", "value": 1.2}],
    "voice": {"formality": "semi-formal", "verbosity": "terse", "regional_voice": "Gujarati-English business register"},
    "sales_persona": SALES_PERSONA}
# D060 (persona v4): the same company but every trait inside a stratum band; STRATA_V4 is what persona_strata would
# assign (hand-written here so the fixture does not depend on the stratifier's seeds).
STRATA_V4 = {
    "tau": {"index": 2, "of": 4, "band": [0.51, 0.74]},
    "max_relaxations": {"index": 2, "of": 4, "band": [2, 2]},
    "patience_rounds": {"index": 1, "of": 4, "band": [2, 2]},
    "decision_randomness": {"index": 1, "of": 4, "band": [0.2, 0.3]},
    "tolerance": {"PRODUCT": {"index": 3, "of": 4, "band": [3.75, 5.0]},
                  "QUALITY": {"index": 0, "of": 4, "band": [0.0, 1.25]},
                  "LOCATION": {"index": 1, "of": 4, "band": [1.25, 2.5]}}}
PERSONA_V4_OUT = {**PERSONA_V3_OUT, "max_relaxations": 2, "patience_rounds": 2, "decision_randomness": 0.25,
                  "tolerance": [{"category": "PRODUCT", "value": 4.1}, {"category": "QUALITY", "value": 1.0},
                              {"category": "LOCATION", "value": 1.5}]}
AVOID_V4 = [{"company_name": "Kaveri Agro Works", "industry": "Agro processing", "city": "Mysuru"},
            {"sales_name": "Arjun", "sales_style": "calm and consultative", "sales_approach": "asks about the use case first"}]
PERSONA_V2_OUT = PERSONA_V3_OUT      # legacy name used by older tests
DOC_LISTING_YES = "Aqua Pumps. Motor: 1.5 HP. We are an ISO 9001:2015 certified company with BIS licence."
DOC_LISTING_NO = "Bharat Motors. Rugged pump with copper winding. Fast delivery across India. Certified quality."
DOC_REQUESTS = [
    DocClaimRequest("P1", "C3", "ISO 9001 certificate", DOC_LISTING_YES),
    DocClaimRequest("P2", "C3", "ISO 9001 certificate", DOC_LISTING_NO),
]

Case = tuple[str, Callable[[Any], Any], dict]


def build_cases() -> list[Case]:
    return [
        ("buyer_opening", lambda r: r.buyer_opening(
            PROFILE, "submersible pump set", 3, "units", None,
            [RequirementStatement(text="Motor rating 1.5 HP.", strength="must"),
             RequirementStatement(text="Delivery within 15 days.", strength="prefer")],
            ctx=CTX.at_turn(0), persona=PERSONA_OBJ),
         {"utterance": OPENING_TEXT}),
        ("buyer_opening", lambda r: r.buyer_opening(
            PROFILE, "submersible pump set", 500.0, "units", 215335.9375,
            [RequirementStatement(text="Motor rating 1.5 HP.", strength="must")],
            ctx=CTX.at_turn(0), persona=PERSONA_OBJ),
         {"utterance": OPENING_FORMATTED_TEXT}),
        ("buyer_reply", lambda r: r.buyer_reply(PROFILE, TRANSCRIPT_NEG, "MIXED", REPLY_ITEMS, ctx=CTX.at_turn(2),
                                         persona=PERSONA_OBJ),
         {"utterance": REPLY_TEXT}),
        ("buyer_reply", lambda r: r.buyer_reply(PROFILE, TRANSCRIPT_ACK, "BYPASSED", PROOF_BUYER_ITEMS,
                                                ctx=CTX.at_turn(4), persona=PERSONA_OBJ),
         {"utterance": PROOF_BUYER_TEXT}),
        ("negotiator_extract", lambda r: r.negotiator_extract(TRANSCRIPT),
         {"items": [
             {"attribute": "motor_rating", "operator": "=", "value": "1.5", "unit": "HP",
              "believed_hardness": "HARD", "is_doc_proof": False, "source_turn": 0},
             {"attribute": "delivery_days", "operator": "<=", "value": "15", "unit": "days",
              "believed_hardness": "SOFT", "is_doc_proof": False, "source_turn": 0}]}),
        ("negotiator_extract", lambda r: r.negotiator_extract(TRANSCRIPT_COMMERCIAL),
         {"items": [
             {"attribute": "budget_inr", "operator": "<=", "value": "345000", "unit": "INR",
              "believed_hardness": "SOFT", "is_doc_proof": False, "source_turn": 0},
             {"attribute": "supplier_state", "operator": "in", "value": "Gujarat, Maharashtra", "unit": None,
              "believed_hardness": "SOFT", "is_doc_proof": False, "source_turn": 0}]}),
        ("negotiator_final", lambda r: r.negotiator_final("recommend", FINAL_FACTS, SALES_PERSONA_LINE, ctx=CTX.at_turn(6)),
         {"utterance": FINAL_RECOMMEND_TEXT}),
        ("negotiator_final", lambda r: r.negotiator_final("best_available", FINAL_FACTS_BEST, SALES_PERSONA_LINE,
                                                          ctx=CTX.at_turn(7)),
         {"utterance": FINAL_BEST_TEXT}),
        ("negotiator_reply", lambda r: r.negotiator_reply(TRANSCRIPT, None, None, EVIDENCE, ctx=CTX.at_turn(1),
                                              sales_persona=SALES_PERSONA, menu_point=MENU_POINT,
                                              allowed_numbers=[15, 25, 2]),
         {"utterance": NEG_REPLY_TEXT, "cited_evidence_ids": ["P1"]}),
        ("negotiator_reply", lambda r: r.negotiator_acknowledge(TRANSCRIPT_PROOF, PROOF_ITEMS, ACK_EVIDENCE,
                                                                ctx=CTX.at_turn(5), sales_persona=SALES_PERSONA),
         {"utterance": ACK_TEXT, "cited_evidence_ids": ["P1"]}),
        ("negotiator_clarify", lambda r: r.negotiator_clarify(TRANSCRIPT, TOPICS, ["motor_rating", "delivery_days"],
                                               ctx=CTX.at_turn(1), sales_persona=SALES_PERSONA,
                                               already_asked=["delivery days"]),
         {"utterance": CLARIFY_TEXT, "asked_topics": ["warranty period", "ISI certification"]}),
        ("buyer_reveal", lambda r: r.buyer_reveal(PROFILE, TRANSCRIPT, REVEAL_REQS, REVEAL_QUESTION, ctx=CTX.at_turn(2),
                                         persona=PERSONA_OBJ, already_stated=["Motor rating 1.5 HP."]),
         {"utterance": REVEAL_TEXT}),
        ("negotiator_interpret", lambda r: r.negotiator_interpret(INTERP_TRANSCRIPT, PROPOSAL_ITEMS),
         {"items": [{"index": 0, "response": "COUNTER", "counter_value": "20 days"}]}),
        ("persona", lambda r: r.persona(
            "centrifugal pump", 3, "units", None, ["PRODUCT", "QUALITY"], scenario_seed=11, scenario_id="syn-v2",
            tau_band=None, bands=BANDS, tightness_target=1, ctx=CTX), PERSONA_V3_OUT),
        ("persona", lambda r: r.persona(
            "centrifugal pump", 3, "units", None, ["PRODUCT", "QUALITY"], scenario_seed=11, scenario_id="syn-v4",
            bands=BANDS, tightness_target=1, ctx=CTX, strata=STRATA_V4, avoid_profiles=AVOID_V4), PERSONA_V4_OUT),
        ("doc_claim", lambda r: r.doc_claims(DOC_REQUESTS),
         {"items": [{"item_id": 0, "claims": True, "quote": "ISO 9001:2015 certified company"},
                    {"item_id": 1, "claims": False, "quote": None}]}),
        ("reveal_check", lambda r: r.reveal_check(OPENING_TEXT, BRIEF_CONSTRAINTS),
         {"revealed_ids": ["C1", "C2"]}),
        ("claim_check", lambda r: r.claim_check(CLAIM_TEXT, EVIDENCE),
         {"claims": [{"claim": "Aqua Pumps is in Rajkot", "evidence_id": "P1"},
                     {"claim": "1.5 HP pump rated 4.2", "evidence_id": "P1"},
                     {"claim": "5 year warranty", "evidence_id": "unsupported"}]}),
    ]


def main(settings_path: str = "config/settings.yaml") -> None:
    os.environ["GTGEN_LLM_MODE"] = "record"
    from .client import FIXTURE_BACKEND, FIXTURE_MODEL, FIXTURE_PROVIDER, LLMClient
    from .runners import LLMRunners
    from gtgen.data.loaders import load_settings

    settings = load_settings(settings_path)
    settings = {**settings, "llm": {**settings["llm"], "deterministic_first": False}}   # D066: record the LLM path
    for name, call, canned in build_cases():
        client = LLMClient(settings, cache_dir=".cache/llm_synth")
        # D058: synthetic fixtures live under the fixed fixture identity, not the production model/provider.
        client._model, client._backend, client._provider = FIXTURE_MODEL, FIXTURE_BACKEND, dict(FIXTURE_PROVIDER)
        client._api_call = lambda *a, _c=canned, **k: json.dumps(_c)  # type: ignore[method-assign]
        res = call(LLMRunners(client, settings))
        print(name, "ok" if res.ok else res.qa_flags, res.llm_call_ids)


if __name__ == "__main__":
    import sys
    main(*sys.argv[1:2])
