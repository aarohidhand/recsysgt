"""D109 tests: government-only wording that Nemotron let through (2026-11-07 tests) is blocked. No network."""
from __future__ import annotations

import os
from pathlib import Path

from gtgen.contracts import BuyerConstraint, FramingDecision, Hardness
from gtgen.data.loaders import load_settings
from gtgen.llm import LLMRunners, find_government_terms, government_terms
from gtgen.llm.synth_fixtures import CTX, SALES_PERSONA

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = load_settings(os.environ.get("GTGEN_TEST_SETTINGS", str(ROOT / "config/settings.yaml")))
TERMS = government_terms(SETTINGS)


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def call(self, prompt_name, prompt_version, messages, response_model, temperature=None, seed=None,
             extra_inputs=None, context=None):
        self.calls.append({"name": prompt_name, "messages": messages})
        return response_model.model_validate(self.responses.pop(0)), f"id{len(self.calls)}"


def test_blocklist_catches_the_reframe_texts_nemotron_wrote():
    # Real commercial_text from out/gt_test (bids GEM/2024/B/5129223 and GEM/2024/B/5002757).
    assert find_government_terms("If the supplier is from a country sharing a land border with India, they must be "
                                 "registered with the Competent Authority.", TERMS)
    assert find_government_terms("Performance guarantee submission within 15 days after completion of work.", TERMS)
    assert find_government_terms("Submit the PBG within 15 days.", TERMS)


def test_final_turn_labels_from_attribute_names_carry_no_government_word():
    # The template fallback printed "payment terms mse days" from the attribute name payment_terms_mse_days.
    n = SETTINGS["llm"]["max_retries_final"] + 1
    fc = FakeClient(*[{"utterance": "Payment for MSE units is open."}] * n)
    facts = {"options": [{"rank": 1, "product": "Falcon Submersible Pump", "supplier": "Jain Sales Corporation",
                          "price_text": "₹ 8,000/Piece", "met": ["budget_inr"], "unmet": ["preferred_states"],
                          "to_confirm": ["payment_terms_mse_days"]}],
             "to_confirm": ["payment_terms_mse_days", "pump_type"]}
    res = LLMRunners(fc, SETTINGS).negotiator_final("best_available", facts, SALES_PERSONA, ctx=CTX.at_turn(3))
    assert any(f.startswith("template_fallback:negotiator_final:") for f in res.qa_flags)
    assert find_government_terms(res.value, TERMS) == []
    assert "payment terms days" in res.value and "pump type" in res.value      # the label stays, minus the word
    assert "mse" not in fc.calls[0]["messages"][0]["content"].lower()           # the LLM never sees it either


def test_reframe_retry_after_a_government_term_offers_drop():
    # D110: GEM/2024/B/5266713 skipped because its value text holds "Performance Guarantee"; Gemma wrote the
    # term 7 times, as "rewrite without them" gave no way out. The retry now names "drop" (reframe.v2 rule).
    c = BuyerConstraint(constraint_id="X_C_000000000001", bid_id="X", golden_param=True, hardness=Hardness.HARD,
                        attribute="bank_guarantee_for_security_deposit", constraint_category="DOCUMENT",
                        buyer_value="Bank Guarantee for Security Deposit cum Performance Guarantee")
    item = {"idx": 0, "golden_param": True, "value": c.buyer_value, "reason": "government-only security"}
    fc = FakeClient({"items": [{**item, "framing_decision": "translate",
                                "commercial_text": "Give a performance guarantee in our format."}]},
                    {"items": [{**item, "framing_decision": "drop"}]})
    res = LLMRunners(fc, SETTINGS).reframe("transformer", [c], ctx=CTX)
    assert 'framing_decision to "drop"' in fc.calls[1]["messages"][-1]["content"]
    assert res.value[0].framing_decision == FramingDecision.DROP
