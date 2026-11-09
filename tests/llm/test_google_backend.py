"""Direct Google backend: Gemini API generateContent with one GEMINI_API_KEY for every role. No network."""
from __future__ import annotations

import copy
import json as jsonlib
from pathlib import Path

import httpx
import pytest

from gtgen.llm import LLMClient

from .test_client import SETTINGS, FakeClock, fake_clock, _Resp, SampleResponse  # noqa: F401

BASE = "https://generativelanguage.googleapis.com/v1beta"
GOOGLE = copy.deepcopy(SETTINGS)
GOOGLE["llm"]["backend"] = "google"
GOOGLE["llm"]["disable_reasoning"] = True
GOOGLE["llm"]["backends"]["google"] = {
    "api": "gemini", "base_url": BASE, "model": "gemma-4-26b-a4b-it",
    "api_key_env": "GEMINI_API_KEY", "structured_output": "prompt_json",
}
NATIVE_OK = {
    "candidates": [{"content": {"parts": [{"text": "let me think", "thought": True},
                                          {"text": '{"greeting":"a","count":1}'}]},
                    "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
}
MESSAGES = [
    {"role": "system", "content": "You are terse."},
    {"role": "user", "content": "Say hello."},
    {"role": "assistant", "content": "{}"},
    {"role": "user", "content": "Try again."},
]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GTGEN_LLM_MODE", "live")
    monkeypatch.delenv("GTGEN_LLM_BACKEND", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-KEY")
    monkeypatch.setenv("OPENROUTER_API_KEY_NEGOTIATOR", "sk-or-neg")   # must be ignored by the google backend


def _send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[SampleResponse, dict, LLMClient]:
    sent: dict = {}

    def post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, body=json, headers=headers)
        return _Resp(200, NATIVE_OK)

    monkeypatch.setattr(httpx, "post", post)
    c = LLMClient(GOOGLE, cache_dir=str(tmp_path / "cache"))
    value, _ = c.call("negotiator_reply", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    return value, sent, c


def test_google_pacer_reserves_input_tokens_only(tmp_path, monkeypatch) -> None:
    # Google's TPM limit counts input tokens only, so the planned completion must not be reserved (D105)
    from gtgen.llm.client import RateLimiter
    reserved: list[int] = []
    monkeypatch.setattr(RateLimiter, "reserve_tokens",
                        lambda self, est, budget, label="", window_s=60.0: reserved.append(est) or 0.0)
    monkeypatch.setattr(httpx, "post", lambda url, json=None, headers=None, timeout=None: _Resp(200, NATIVE_OK))
    s = copy.deepcopy(GOOGLE)
    s["llm"].update(tpm_limit=16000, batch_completion_target=3000)
    c = LLMClient(s, cache_dir=str(tmp_path / "cache"))
    c.call("negotiator_reply", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    assert reserved == [c._requests[-1]["est_input_tokens"]]


def test_google_backend_posts_generate_content_with_gemini_key(tmp_path, monkeypatch) -> None:
    _, sent, c = _send(tmp_path, monkeypatch)
    assert sent["url"] == f"{BASE}/models/gemma-4-26b-a4b-it:generateContent"
    assert sent["headers"]["x-goog-api-key"] == "AIza-test-KEY"
    assert "Authorization" not in sent["headers"]
    assert c.events[0]["api_key_env"] == "GEMINI_API_KEY"


def test_google_body_is_native_gemini_shape(tmp_path, monkeypatch) -> None:
    _, sent, _ = _send(tmp_path, monkeypatch)
    body = sent["body"]
    assert not {"model", "messages", "provider", "reasoning", "response_format"} & set(body)
    assert body["systemInstruction"]["parts"][0]["text"].startswith("You are terse.")
    assert "JSON Schema" in body["systemInstruction"]["parts"][0]["text"]      # prompt_json: schema in the prompt
    assert [m["role"] for m in body["contents"]] == ["user", "model", "user"]
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert body["generationConfig"]["temperature"] == 0.0
    assert body["generationConfig"]["maxOutputTokens"] > 0


def test_google_answer_skips_thought_parts(tmp_path, monkeypatch) -> None:
    value, _, _ = _send(tmp_path, monkeypatch)
    assert (value.greeting, value.count) == ("a", 1)


def test_google_key_is_redacted_from_errors(tmp_path, monkeypatch) -> None:
    def post(url, json=None, headers=None, timeout=None):
        return _Resp(400, text="bad key AIza-test-KEY")

    monkeypatch.setattr(httpx, "post", post)
    c = LLMClient(GOOGLE, cache_dir=str(tmp_path / "cache"))
    with pytest.raises(Exception) as exc:
        c.call("reframe", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    assert "AIza-test-KEY" not in str(exc.value)
    assert "AIza-test-KEY" not in jsonlib.dumps(c.events, default=str)
