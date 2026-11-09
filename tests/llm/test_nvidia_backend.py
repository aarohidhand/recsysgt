"""D108: NVIDIA API catalog backend (OpenAI-style chat/completions, one NVIDIA_API_KEY for every role) and the
provenance model/backend. No network."""
from __future__ import annotations

import copy
from pathlib import Path

import httpx
import pytest

from gtgen.llm import LLMClient
from gtgen.llm.client import active_llm

from . import test_client as tc

fake_clock = tc.fake_clock          # autouse there; re-exported so no test here really sleeps

BASE = "https://integrate.api.nvidia.com/v1"
MODEL = "deepseek-ai/deepseek-v4.1-flash"
NVIDIA = copy.deepcopy(tc.SETTINGS)
NVIDIA["llm"].update(backend="nvidia", disable_reasoning=True, tpm_limit=5300)
NVIDIA["llm"]["backends"]["nvidia"] = {
    "base_url": BASE, "model": MODEL, "api_key_env": "NVIDIA_API_KEY", "structured_output": "prompt_json",
    "disable_reasoning": False, "extra_body": {"chat_template_kwargs": {"thinking": False}},
    "rate_limit": {"requests_per_minute": 40, "min_interval_s": 1.5}, "tpm_limit": None,
}
MESSAGES = [{"role": "user", "content": "Say hello."}]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GTGEN_LLM_MODE", "live")
    monkeypatch.delenv("GTGEN_LLM_BACKEND", raising=False)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-KEY")
    monkeypatch.setenv("OPENROUTER_API_KEY_NEGOTIATOR", "sk-or-neg")   # must be ignored by the nvidia backend


def _send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, LLMClient]:
    sent: dict = {}

    def post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, body=json, headers=headers)
        return tc._Resp(200, tc._OK)

    monkeypatch.setattr(httpx, "post", post)
    c = LLMClient(NVIDIA, cache_dir=str(tmp_path / "cache"))
    value, _ = c.call("negotiator_reply", 1, MESSAGES, tc.SampleResponse, temperature=0.0, seed=1)
    assert value.greeting == "a"
    return sent, c


def test_nvidia_backend_posts_chat_completions_with_nvidia_key(tmp_path, monkeypatch) -> None:
    sent, c = _send(tmp_path, monkeypatch)
    assert sent["url"] == f"{BASE}/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer nvapi-test-KEY"
    assert sent["body"]["model"] == MODEL
    assert c.events[0]["api_key_env"] == "NVIDIA_API_KEY"


def test_nvidia_body_has_no_openrouter_fields_and_carries_extra_body(tmp_path, monkeypatch) -> None:
    body = _send(tmp_path, monkeypatch)[0]["body"]
    assert "provider" not in body and "reasoning" not in body      # OpenRouter-only fields
    assert "response_format" not in body                           # prompt_json: schema goes in the prompt
    assert body["chat_template_kwargs"] == {"thinking": False}


def test_nvidia_backend_overrides_rate_limit_and_turns_token_pacer_off(tmp_path) -> None:
    c = LLMClient(NVIDIA, cache_dir=str(tmp_path / "cache"))
    assert c._min_interval_s == 1.5
    assert c._tpm_budget is None                                   # llm.tpm_limit is Google's, not NVIDIA's


def test_openrouter_backend_still_sends_reasoning_off(tmp_path, monkeypatch) -> None:
    sent: dict = {}
    monkeypatch.setenv("OPENROUTER_API_KEY_NEGOTIATOR", "sk-or-neg")
    monkeypatch.setattr(httpx, "post", lambda url, json=None, headers=None, timeout=None:
                        sent.update(body=json) or tc._Resp(200, tc._OK))
    s = copy.deepcopy(NVIDIA)
    s["llm"]["backend"] = "openrouter"
    LLMClient(s, cache_dir=str(tmp_path / "cache")).call(
        "negotiator_reply", 1, MESSAGES, tc.SampleResponse, temperature=0.0, seed=1)
    assert sent["body"]["reasoning"] == {"enabled": False}
    assert "chat_template_kwargs" not in sent["body"]


def test_active_llm_follows_the_backend_env_override(monkeypatch) -> None:
    # Provenance must name the backend and model that really ran, not the settings-file defaults.
    assert active_llm(NVIDIA) == ("nvidia", MODEL)
    monkeypatch.setenv("GTGEN_LLM_BACKEND", "openrouter")
    assert active_llm(NVIDIA) == ("openrouter", "google/gemma-4-26b-a4b-it:free")
