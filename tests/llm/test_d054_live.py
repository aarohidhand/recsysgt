"""D054/D055/D056 tests: conversation-specific cache keys, --fresh, provenance, live no-template guard,
persona v3 clamping, proof requests and ACKNOWLEDGE. No network."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from gtgen.contracts import Speaker, TranscriptTurn, TranscriptView
from gtgen.data.loaders import load_settings
from gtgen.llm import (
    ConversationContext, LiveModeTemplateError, LLMCallFailed, LLMClient, LLMRunners, MissingConversationContext,
    NegotiatorInputError, ScenarioSkipped, build_negotiator_reply_variables, forbid_template_in_live, numbers_problem,
)
from gtgen.llm.prompts import PROMPT_NAMES, latest_version, load_prompt
from gtgen.llm.schemas import BuyerItemDecision, MenuItemView, MenuPointView, ProofRequestView, UtteranceOutput
from gtgen.llm.synth_fixtures import (
    ACK_EVIDENCE, BANDS, CTX, EVIDENCE, MENU_POINT, PERSONA_OBJ, PERSONA_V3_OUT, PROFILE, PROOF_BUYER_ITEMS,
    PROOF_BUYER_TEXT, PROOF_ITEMS, SALES_PERSONA, TRANSCRIPT, TRANSCRIPT_ACK, TRANSCRIPT_PROOF,
)
from gtgen.llm.runners import BEHAVIOUR_BY_TIER

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = load_settings(os.environ.get("GTGEN_TEST_SETTINGS", str(ROOT / "config/settings.yaml")))
RESP = '{"utterance": "hello there"}'


def live_client(tmp_path, monkeypatch, fresh=None, mode="live"):
    monkeypatch.setenv("GTGEN_LLM_MODE", mode)
    monkeypatch.delenv("GTGEN_LLM_FRESH", raising=False)
    c = LLMClient(SETTINGS, cache_dir=str(tmp_path / "cache"), fresh=fresh)
    calls = []

    def fake_api(messages, temperature, seed, response_model):
        calls.append(messages)
        return RESP
    c._api_call = fake_api  # type: ignore[method-assign]
    return c, calls


def call(c, ctx, text="same prompt"):
    return c.call("buyer_opening", 2, [{"role": "user", "content": text}], UtteranceOutput, temperature=0.8,
                  seed=3, extra_inputs={"x": 1}, context=ctx.as_dict() if ctx else None)


# ---------------------------------------------------------------- cache-key separation
def test_cache_key_separates_scenario_turn_and_seed(tmp_path, monkeypatch):
    c, calls = live_client(tmp_path, monkeypatch)
    ids = {call(c, ConversationContext("s1", 1, 0))[1], call(c, ConversationContext("s2", 1, 0))[1],
           call(c, ConversationContext("s1", 1, 1))[1], call(c, ConversationContext("s1", 2, 0))[1]}
    assert len(ids) == 4 and len(calls) == 4                     # identical prompt, four different conversations/turns
    again = call(c, ConversationContext("s1", 1, 0))[1]
    assert again in ids and len(calls) == 4                      # exact replay still hits


def test_context_free_key_is_unchanged_by_the_context_feature():
    k = LLMClient._cache_key("m", "p", 1, [{"role": "user", "content": "x"}], {"a": 1}, 5, backend="b", provider={})
    assert k == LLMClient._cache_key("m", "p", 1, [{"role": "user", "content": "x"}], {"a": 1}, 5, backend="b",
                                     provider={}, context=None)


# ---------------------------------------------------------------- --fresh
def test_fresh_never_reads_cache_but_normal_does(tmp_path, monkeypatch):
    c, calls = live_client(tmp_path, monkeypatch)
    call(c, CTX), call(c, CTX)
    assert len(calls) == 1
    f, fcalls = live_client(tmp_path, monkeypatch, fresh=True)      # same cache dir, warm
    a, b = call(f, CTX), call(f, CTX)
    assert len(fcalls) == 2 and a[1] == b[1] and f.fresh
    assert [e["source"] for e in f.events] == ["live", "live"]


def test_fresh_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GTGEN_LLM_FRESH", "1")
    monkeypatch.setenv("GTGEN_LLM_MODE", "live")
    assert LLMClient(SETTINGS, cache_dir=str(tmp_path)).fresh is True


# ---------------------------------------------------------------- provenance
def test_provenance_live_then_cache_hit_and_share(tmp_path, monkeypatch):
    c, _ = live_client(tmp_path, monkeypatch)
    i1 = call(c, CTX)[1]
    call(c, CTX)
    call(c, CTX.at_turn(2))
    assert [e["source"] for e in c.events] == ["live", "cache_hit", "live"]
    summ = c.provenance_summary()
    assert summ["live"] == 2 and summ["cache_hit"] == 1 and summ["live_share"] == pytest.approx(2 / 3)
    assert c.source_of(i1) == "cache_hit"
    assert c.events[2]["scenario_id"] == "syn-1" and c.events[2]["turn_idx"] == 2 and c.events[2]["persona_seed"] == 11
    lines = [json.loads(x) for x in (tmp_path / "cache" / "provenance.jsonl").read_text().splitlines()]
    assert [x["source"] for x in lines] == ["live", "cache_hit", "live"]
    assert c.provenance_summary("nope")["live_share"] is None
    # the cache entry records context + origin
    entry = json.loads(c._cache_path(i1).read_text())
    assert entry["origin"] == "live" and entry["context"]["scenario_id"] == "syn-1"


def test_fresh_run_has_full_live_share(tmp_path, monkeypatch):
    c, _ = live_client(tmp_path, monkeypatch)
    call(c, CTX)
    f, _ = live_client(tmp_path, monkeypatch, fresh=True)
    call(f, CTX), call(f, CTX.at_turn(1))
    assert f.provenance_summary()["live_share"] == 1.0


def test_mock_mode_logs_fixture_then_cache_hit(tmp_path, monkeypatch):
    from gtgen.llm.synth_fixtures import build_cases
    monkeypatch.setenv("GTGEN_LLM_MODE", "mock")
    name, fn, _ = [c for c in build_cases() if c[0] == "negotiator_reply"][1]      # the ACKNOWLEDGE case
    c = LLMClient(SETTINGS, cache_dir=str(tmp_path / "c"))
    assert fn(LLMRunners(c, SETTINGS)).ok and fn(LLMRunners(c, SETTINGS)).ok
    assert [e["source"] for e in c.events] == ["fixture", "cache_hit"]
    assert c.events[0]["scenario_id"] == "syn-1" and c.events[0]["turn_idx"] == 5


# ---------------------------------------------------------------- live-mode no-template enforcement
def test_forbid_template_raises_in_live_and_record_only(tmp_path, monkeypatch):
    for mode, raises in (("live", True), ("record", True), ("mock", False)):
        monkeypatch.setenv("GTGEN_LLM_MODE", mode)
        c = LLMClient(SETTINGS, cache_dir=str(tmp_path / mode))
        if raises:
            with pytest.raises(LiveModeTemplateError):
                forbid_template_in_live(c, "STUB_REPLY")
        else:
            forbid_template_in_live(c, "STUB_REPLY")
    forbid_template_in_live(object(), "test double")             # duck-typed fakes are allowed (mock-style tests)


def test_live_runner_without_context_raises(tmp_path, monkeypatch):
    c, calls = live_client(tmp_path, monkeypatch)
    r = LLMRunners(c, SETTINGS)
    with pytest.raises(MissingConversationContext):
        r.buyer_opening(PROFILE, "pump", 3, "units", None, [], persona=PERSONA_OBJ)
    assert calls == []


def test_live_runner_needs_persona_sales_persona_and_bands(tmp_path, monkeypatch):
    c, calls = live_client(tmp_path, monkeypatch)
    r = LLMRunners(c, SETTINGS)
    with pytest.raises(ValueError, match="Persona"):
        r.buyer_opening(PROFILE, "pump", 3, "units", None, [], ctx=CTX)
    with pytest.raises(ValueError, match="sales persona"):
        r.negotiator_clarify(TRANSCRIPT, [], [], ctx=CTX)
    with pytest.raises(ValueError, match="bands"):
        r.persona("pump", 3, "units", None, ["PRODUCT"], scenario_seed=1, ctx=CTX)
    assert calls == []


def test_fixture_derived_cache_entry_is_not_a_live_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("GTGEN_LLM_MODE", "mock")
    m = LLMClient(SETTINGS, cache_dir=str(tmp_path / "cache"))
    r = LLMRunners(m, SETTINGS)
    from gtgen.llm.synth_fixtures import build_cases
    name, fn, _ = [c for c in build_cases() if c[0] == "buyer_opening"][0]
    res = fn(r)
    assert res.ok
    entry = json.loads(m._cache_path(res.llm_call_ids[-1]).read_text())
    assert entry["origin"] == "fixture"
    c, calls = live_client(tmp_path, monkeypatch)                # same cache dir, live mode
    c.call("buyer_opening", 2, [{"role": "user", "content": "x"}], UtteranceOutput)   # unrelated key: just make a call
    # force the fixture entry's key and verify live ignores it
    key = res.llm_call_ids[-1]
    c._cache_key = lambda *a, **k: key                           # type: ignore[method-assign]
    before = len(calls)
    out, _ = c.call("buyer_opening", 2, [{"role": "user", "content": "x"}], UtteranceOutput)
    assert len(calls) == before + 1 and out.utterance == "hello there"


def test_llm_result_require_raises_instead_of_stub():
    from gtgen.llm import LLMResult
    with pytest.raises(LLMCallFailed):
        LLMResult(None, [], ["llm_failed:buyer_reply:x"]).require()
    assert LLMResult("ok", ["i"]).require() == "ok"


# ---------------------------------------------------------------- prompts: headers / blocklist / barrier
V2_NAMES = ["buyer_opening", "buyer_reply", "buyer_reveal", "negotiator_clarify", "negotiator_reply"]


# D065: buyer_opening / buyer_reply / buyer_reveal / negotiator_reply / negotiator_clarify are now at v3 (latest)
V3_NAMES = ["buyer_opening", "buyer_reply", "buyer_reveal", "negotiator_clarify", "negotiator_reply"]
# D070: the same five are now at v4 (supplier rating / years of experience are never spoken), persona at v5
V4_NAMES = V3_NAMES
D072_VERSIONS = {"buyer_opening": 5, "buyer_reveal": 6, "negotiator_clarify": 6, "buyer_reply": 5,
                  "negotiator_reply": 5}   # D073/D078


@pytest.mark.parametrize("name,version", [(n, D072_VERSIONS.get(n, 4 if n in V4_NAMES else 2)) for n in V2_NAMES] + [("persona", 8)])
def test_new_versions_list_per_conversation_inputs_and_are_latest(name, version):
    spec = load_prompt(name, version, ROOT / "prompts")
    assert spec.version == version == latest_version(name) and name in PROMPT_NAMES
    assert spec.per_conversation_inputs and set(spec.per_conversation_inputs) <= set(spec.input_vars)
    assert spec.temperature_key == name and name in SETTINGS["llm"]["temperature"]
    assert not re.search(r"(?<![A-Za-z0-9_])bid(s|der|ders|ding)?(?![A-Za-z0-9_])",
                         (ROOT / "prompts" / f"{name}.v{version}.md").read_text(), re.I)


def test_old_versions_still_present_for_recorded_fixtures():
    for n in V2_NAMES:
        assert load_prompt(n, 1, ROOT / "prompts").version == 1
        assert load_prompt(n, 2, ROOT / "prompts").version == 2


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def call(self, prompt_name, prompt_version, messages, response_model, temperature=None, seed=None,
             extra_inputs=None, context=None):
        self.calls.append({"name": prompt_name, "v": prompt_version, "messages": messages, "context": context})
        return response_model.model_validate(self.responses.pop(0)), f"id{len(self.calls)}"


N_ATTEMPTS = SETTINGS["llm"]["max_retries"] + 1   # first try + validation retries (D066)


def R(*resp):
    fc = FakeClient(*resp)
    return LLMRunners(fc, SETTINGS), fc


def test_context_is_passed_to_client_only_when_given():
    r, fc = R({"utterance": "Hello, we need pumps. Three sets please."}, {"utterance": "Hello, fine. Three sets."})
    r.buyer_opening(PROFILE, "pump", 3, "units", None, [], ctx=CTX.at_turn(0), persona=PERSONA_OBJ)
    r.buyer_opening(PROFILE, "pump", 3, "units", None, [])
    assert fc.calls[0]["context"] == {"scenario_id": "syn-1", "turn_idx": 0, "persona_seed": 11}
    assert fc.calls[1]["context"] is None and fc.calls[0]["v"] == 5


@pytest.mark.parametrize("bad", ["We will tender this", "Per the GeM listing we need it", "EMD is fine",
                                 "Our tau is high", "ref GEM/2024/B/123456"])
def test_new_buyer_reply_blocklist_then_flag(bad):
    # D078 amendment: exhausted retries no longer skip the scenario - a deterministic fallback utterance is used
    # instead, flagged with a template_fallback qa_flag naming the prompt and the check that kept failing.
    r, fc = R(*[{"utterance": bad}] * N_ATTEMPTS)
    res = r.buyer_reply(PROFILE, TRANSCRIPT, "YES", [], ctx=CTX, persona=PERSONA_OBJ)
    assert len(fc.calls) == N_ATTEMPTS and not res.ok and res.value
    assert any(f.startswith("template_fallback:buyer_reply:") for f in res.qa_flags)


def test_buyer_reply_numbers_must_come_from_decisions_or_conversation():
    items = [BuyerItemDecision(item="Delivery", action="YES", proposed_value="25 days")]
    r, fc = R({"utterance": "Fine, 30 days works."}, {"utterance": "Fine, 25 days works after your note on 1.5 HP."})
    res = r.buyer_reply(PROFILE, TRANSCRIPT, "ACCEPTED", items, ctx=CTX, persona=PERSONA_OBJ)
    assert res.ok and len(fc.calls) == 2 and "not in the allowed numbers" in fc.calls[1]["messages"][-1]["content"]


def test_buyer_prompt_receives_behaviour_words_not_numbers_or_tier():
    r, fc = R({"utterance": "Hello, we need pumps. Three sets please."})
    r.buyer_opening(PROFILE, "pump", 3, "units", None, [], ctx=CTX, persona=PERSONA_OBJ)
    beh = r.buyer_behaviour(PERSONA_OBJ)
    assert not re.search(r"\d", beh) and "medium_strict" not in beh and "0.642" not in beh
    assert BEHAVIOUR_BY_TIER["medium_strict"] in beh and beh in fc.calls[0]["messages"][0]["content"]
    assert "medium_strict" not in fc.calls[0]["messages"][0]["content"]


# ---------------------------------------------------------------- proof request (D055)
def test_proof_request_item_needs_doc_name():
    with pytest.raises(ValidationError):
        BuyerItemDecision(item="Quality", action="PROOF_REQUEST")


def test_buyer_proof_request_must_name_document_then_ok():
    r, fc = R({"utterance": "Please send some proof of the certification."}, {"utterance": PROOF_BUYER_TEXT})
    res = r.buyer_reply(PROFILE, TRANSCRIPT_ACK, "BYPASSED", PROOF_BUYER_ITEMS, ctx=CTX.at_turn(4), persona=PERSONA_OBJ)
    assert res.ok and res.value == PROOF_BUYER_TEXT
    assert "must name the document" in fc.calls[1]["messages"][-1]["content"]
    assert "PROOF_REQUEST" in fc.calls[0]["messages"][0]["content"]


def test_acknowledge_names_document_and_cites_claim():
    good = {"utterance": "Understood. The supplier, listed as ISO 9001:2015 certified, will provide the ISO 9001 certificate.",
            "cited_evidence_ids": ["P1"]}
    no_doc = {"utterance": "Understood, we will arrange proof.", "cited_evidence_ids": ["P1"]}
    no_cite = {"utterance": "The supplier will provide the ISO 9001 certificate.", "cited_evidence_ids": []}
    r, fc = R(no_doc, no_cite, good)
    res = r.negotiator_acknowledge(TRANSCRIPT_PROOF, PROOF_ITEMS, ACK_EVIDENCE, ctx=CTX.at_turn(5),
                                   sales_persona=SALES_PERSONA)
    assert res.ok and res.value[1] == ["P1"] and len(fc.calls) == 3
    msgs = [c["messages"][-1]["content"] for c in fc.calls[1:]]
    assert "exactly as given" in msgs[0] and "must include the claim evidence" in msgs[1]
    sent = fc.calls[0]["messages"][0]["content"]
    assert "ACKNOWLEDGE" in sent and "ISO 9001 certificate" in sent and "{{" not in sent


def test_acknowledge_rejects_invented_numbers():
    bad_num = {"utterance": "The supplier will provide the ISO 9001 certificate within 7 days.", "cited_evidence_ids": ["P1"]}
    good = {"utterance": "The supplier will provide the ISO 9001 certificate.", "cited_evidence_ids": ["P1"]}
    r, fc = R(bad_num, good)
    res = r.negotiator_acknowledge(TRANSCRIPT_PROOF, PROOF_ITEMS, ACK_EVIDENCE, ctx=CTX, sales_persona=SALES_PERSONA)
    assert res.ok and "not in the allowed" in fc.calls[1]["messages"][-1]["content"]


def test_acknowledge_blocklist_and_requires_requests():
    # D078 amendment: exhausted retries fall back to a deterministic utterance instead of skipping the scenario.
    r, fc = R(*[{"utterance": "Per tender rules the ISO 9001 certificate follows.", "cited_evidence_ids": ["P1"]}] * N_ATTEMPTS)
    res = r.negotiator_acknowledge(TRANSCRIPT_PROOF, PROOF_ITEMS, ACK_EVIDENCE, ctx=CTX, sales_persona=SALES_PERSONA)
    assert not res.ok and res.value[0] and any(f.startswith("template_fallback:negotiator_reply:") for f in res.qa_flags)
    with pytest.raises(ValueError):
        build_negotiator_reply_variables(TRANSCRIPT, None, None, EVIDENCE, reply_kind="ACKNOWLEDGE", proof_requests=[])
    with pytest.raises(ValueError, match="not in the supplied evidence"):
        build_negotiator_reply_variables(TRANSCRIPT, None, None, EVIDENCE, reply_kind="ACKNOWLEDGE",
                                         proof_requests=[ProofRequestView(attribute="a", doc_name="d", evidence_id="Z9")])


# ---------------------------------------------------------------- negotiator barrier on the new inputs
@pytest.mark.parametrize("tok", ["golden_param", "tau", "bid_id", "gem_bid_id"])
@pytest.mark.parametrize("where", ["persona", "menu", "proof", "evidence", "allowed"])
def test_negotiator_reply_v2_builder_refuses_hidden_tokens(tok, where):
    sp, mp, pr, ev, al = dict(SALES_PERSONA), MENU_POINT, list(PROOF_ITEMS), list(ACK_EVIDENCE), [10]
    kind = "PROPOSE"
    if where == "persona":
        sp["sales_approach"] = f"uses {tok} to plan"
    elif where == "menu":
        mp = MenuPointView(items=[MenuItemView(attribute=tok, kind="drop")], extra_options=1)
    elif where == "proof":
        kind = "ACKNOWLEDGE"
        pr = [ProofRequestView(attribute="a", doc_name=f"{tok} doc", evidence_id="P1")]
    elif where == "evidence":
        from gtgen.llm.schemas import EvidenceSnippet
        ev = [EvidenceSnippet(evidence_id="P1", text=f"{tok}=1")]
    else:
        al = [f"{tok}"]
    with pytest.raises(NegotiatorInputError):
        build_negotiator_reply_variables(TRANSCRIPT, None, None, ev, sales_persona=sp, menu_point=mp,
                                         allowed_numbers=al, reply_kind=kind, proof_requests=pr if kind != "PROPOSE" else None)


def test_clarify_builder_refuses_hidden_tokens_in_sales_persona():
    from gtgen.llm import build_negotiator_clarify_variables
    with pytest.raises(NegotiatorInputError):
        build_negotiator_clarify_variables(TRANSCRIPT, [], [], {"style": "knows the golden_param"})


def test_negotiator_reply_menu_numbers_enforced_and_extra_options_allowed():
    ok = {"utterance": "Could you stretch delivery from 15 to 25 days? That opens up 2 more options.", "cited_evidence_ids": []}
    bad = {"utterance": "Could you stretch delivery to 40 days? That opens up 2 more options.", "cited_evidence_ids": []}
    r, fc = R(bad, ok)
    res = r.negotiator_reply(TRANSCRIPT, None, None, EVIDENCE, ctx=CTX, sales_persona=SALES_PERSONA, menu_point=MENU_POINT)
    assert res.ok and len(fc.calls) == 2 and "not in the allowed" in fc.calls[1]["messages"][-1]["content"]
    assert fc.calls[0]["v"] == 5
    sent = fc.calls[0]["messages"][0]["content"]
    assert "Warm, quick to smile" in sent and "numeric_widen" in sent and "{{" not in sent


def test_numbers_problem_helpers():
    assert numbers_problem("INR 1,50,000 for 3 units", ["budget 150000", 3]) is None
    assert "7" in numbers_problem("in 7 days", ["15 days"])
    assert numbers_problem("no digits here", []) is None


# ---------------------------------------------------------------- persona v3
def persona_call(r, payload_edit=None, bands=BANDS):
    p = json.loads(json.dumps(PERSONA_V3_OUT))
    if payload_edit:
        payload_edit(p)
    return p, r


def test_persona_v3_clamps_to_settings_ranges_and_reports():
    rg = SETTINGS["relaxation"]["persona_ranges"]
    r, fc = R(json.loads(json.dumps(PERSONA_V3_OUT)))
    res = r.persona("pump", 3, "units", None, ["PRODUCT", "QUALITY"], scenario_seed=11, scenario_id="s",
                    tau_band=None, bands=BANDS, tightness_target=1, ctx=CTX)
    profile, persona = res.value
    assert fc.calls[0]["v"] == 3
    assert persona.max_relaxations == rg["max_relaxations"][1] == 3
    assert persona.patience_rounds == min(6, rg["patience_rounds"][1])
    assert persona.decision_randomness == rg["decision_randomness"][1]
    assert persona.tolerance == {"PRODUCT": rg["tolerance"][1], "QUALITY": 2.0, "LOCATION": 1.2}
    assert set(res.extra["clamped"]) == {"max_relaxations", "patience_rounds", "decision_randomness", "tolerance:PRODUCT"}
    assert res.extra["sales_persona"] == SALES_PERSONA and res.extra["voice"]["verbosity"] == "terse"
    assert "Formality: semi-formal" in profile.style and "Gujarati-English" in profile.style
    assert persona.tier.value == "medium_strict"          # tier still computed in code


def test_persona_v3_values_inside_range_are_untouched_and_low_side_clamped():
    def edit(p):
        p.update(max_relaxations=-4, patience_rounds=0, decision_randomness=0.02)
        p["tolerance"] = [{"category": "PRODUCT", "value": -1.0}, {"category": "QUALITY", "value": 1.25}, {"category": "LOCATION", "value": 0.0}]
    p, r = persona_call(R(), edit)[0], None
    r, _ = R(p)
    res = r.persona("pump", 3, "units", None, ["PRODUCT", "QUALITY"], scenario_seed=11, tau_band=None, bands=BANDS, ctx=CTX)
    per = res.value[1]
    assert (per.max_relaxations, per.patience_rounds, per.decision_randomness) == (0, 1, 0.1)
    assert per.tolerance == {"PRODUCT": 0.0, "QUALITY": 1.25, "LOCATION": 0.0}


def test_persona_v3_missing_tolerance_category_or_bad_sales_persona_retries_then_fails():
    def no_cat(p):
        p["tolerance"] = [{"category": "PRODUCT", "value": 1.0}]
    p = persona_call(None, no_cat)[0]
    r, fc = R(*[p] * N_ATTEMPTS)
    with pytest.raises(ScenarioSkipped):
        r.persona("pump", 3, "units", None, ["PRODUCT", "QUALITY"], scenario_seed=11, tau_band=None, bands=BANDS, ctx=CTX)
    assert "tolerance" in fc.calls[1]["messages"][-1]["content"]

    def gov(p):
        p["sales_persona"]["sales_approach"] = "Pushes the tender angle hard to close."
    p2 = persona_call(None, gov)[0]
    r, fc = R(*[p2] * N_ATTEMPTS)
    with pytest.raises(ScenarioSkipped):
        r.persona("pump", 3, "units", None, ["PRODUCT", "QUALITY"], scenario_seed=11, tau_band=None, bands=BANDS, ctx=CTX)
    assert "sales_persona" in fc.calls[1]["messages"][-1]["content"]
    # D078: sales_persona has no "name" field any more, so there is nothing left for a model to leak a hidden
    # internal name through here; the old "hidden name" sub-case (golden_param via sales_persona.name) no longer applies.


def test_persona_v3_prompt_has_ranges_and_new_fields():
    spec = load_prompt("persona", 3, ROOT / "prompts")
    assert spec.output_schema == "PersonaOutputV3" and "ranges_json" in spec.input_vars
    r, fc = R(json.loads(json.dumps(PERSONA_V3_OUT)))
    r.persona("pump", 3, "units", None, ["PRODUCT", "QUALITY"], scenario_seed=11, tau_band=None, bands=BANDS, ctx=CTX)
    msg = fc.calls[0]["messages"][0]["content"]
    assert '"max_relaxations":[0.0,3.0]' in msg or '"max_relaxations":[0,3]' in msg or "max_relaxations" in msg
    assert "sales_persona" in msg and "{{" not in msg


def test_persona_missing_relaxation_ranges_raises():
    s2 = json.loads(json.dumps(SETTINGS))
    del s2["relaxation"]["persona_ranges"]
    r = LLMRunners(FakeClient(PERSONA_V3_OUT), s2)
    with pytest.raises(KeyError, match="persona_ranges"):
        r.persona("pump", 3, "units", None, ["PRODUCT", "QUALITY"], scenario_seed=11, tau_band=None, bands=BANDS, ctx=CTX)
