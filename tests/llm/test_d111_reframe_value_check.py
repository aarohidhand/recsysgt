"""D111 tests: the reframe value check (D066) rejected correct texts and skipped 4 bids (2026-11-07/08). No network.

Each case is the real constraint and the real text Gemma wrote (from .cache/llm), plus a guard that a text
without the value is still rejected."""
from __future__ import annotations

import os
from pathlib import Path

from gtgen.contracts import BuyerConstraint, FramingDecision, Hardness
from gtgen.data.loaders import load_settings
from gtgen.llm import LLMRunners, government_terms
from gtgen.llm.runners import reframe_value_problem
from gtgen.llm.synth_fixtures import CTX

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = load_settings(os.environ.get("GTGEN_TEST_SETTINGS", str(ROOT / "config/settings.yaml")))
TERMS = government_terms(SETTINGS)
DROP_HINT = 'framing_decision to "drop"'


def bc(**kw) -> BuyerConstraint:
    return BuyerConstraint(constraint_id="X_C_000000000001", bid_id="X", golden_param=False, hardness=Hardness.SOFT,
                           **kw)


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def call(self, prompt_name, prompt_version, messages, response_model, temperature=None, seed=None,
             extra_inputs=None, context=None):
        self.calls.append({"name": prompt_name, "messages": messages})
        return response_model.model_validate(self.responses.pop(0)), f"id{len(self.calls)}"


def test_hyphen_range_is_met_by_a_worded_range():
    # GEM/2024/B/4825544
    c = bc(attribute="capacity", constraint_category="CAPACITY", operator="in", unit="ml", allowed_values=["1001-1250"])
    assert reframe_value_problem(c, "Capacity should be between 1001 and 1250 ml.", TERMS) is None
    assert reframe_value_problem(c, "Capacity should be 1001 to 1250 ml.", TERMS) is None
    assert reframe_value_problem(c, "Capacity should be between 1001 and 1500 ml.", TERMS)


def test_small_number_written_as_a_word():
    # GEM/2024/B/5528492
    c = bc(attribute="sample_requirement", constraint_category="OTHER", operator="required", unit="sample",
           buyer_value="1")
    assert reframe_value_problem(c, "One sample is required.", TERMS) is None
    assert reframe_value_problem(c, "Samples are required.", TERMS)
    assert reframe_value_problem(c, "Two samples are required.", TERMS)


def test_range_does_not_need_the_standard_number():
    # GEM/2024/B/5542314: "744" is the standard (IS 744), not part of the range.
    c = bc(attribute="average_wool_fibre_diameter", constraint_category="TECHNICAL", operator="in", unit="Micron",
           allowed_values=["31.00 to 32.69 (as per IS 744)"])
    assert reframe_value_problem(c, "Average wool fibre diameter should be 31.00 to 32.69 Micron.", TERMS) is None
    assert reframe_value_problem(c, "Average wool fibre diameter should be between 31.00 and 32.69 Micron.",
                                 TERMS) is None
    assert reframe_value_problem(c, "Average wool fibre diameter is given in Micron.", TERMS)


def test_government_word_in_the_value_does_not_block_the_check():
    # GEM/2023/B/4142698: the value is a tender form label; "Bidder" is blocked, so the text cannot hold it.
    c = bc(attribute="technical_specification_acceptance", constraint_category="TECHNICAL", operator="=",
           buyer_value='Whether all the "Technical Specification of material" in the NIT is accepted',
           allowed_values=["Bidder’s Compliance / Comment (YES/No)"])
    assert reframe_value_problem(c, "Confirm acceptance of all technical specifications with YES/No.", TERMS) is None
    miss = reframe_value_problem(c, "Confirm acceptance of all technical specifications.", TERMS)
    assert miss and DROP_HINT in miss


def test_reframe_retry_names_drop_and_accepts_the_corrected_text():
    c = bc(attribute="technical_specification_acceptance", constraint_category="TECHNICAL", operator="=",
           buyer_value="Whether all the technical specifications are accepted",
           allowed_values=["Bidder’s Compliance / Comment (YES/No)"])
    item = {"idx": 0, "golden_param": False, "operator": "=", "value": c.buyer_value, "framing_decision": "translate",
            "reason": "acceptance of the specification"}
    fc = FakeClient({"items": [{**item, "commercial_text": "Confirm acceptance of all technical specifications."}]},
                    {"items": [{**item, "commercial_text": "Confirm acceptance of all technical specifications "
                                                           "with YES/No."}]})
    res = LLMRunners(fc, SETTINGS).reframe("pump", [c], ctx=CTX)
    assert DROP_HINT in fc.calls[1]["messages"][-1]["content"]
    assert len(fc.calls) == 2 and res.value[0].framing_decision == FramingDecision.TRANSLATE
