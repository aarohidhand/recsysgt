"""Tests for LLMClient — all run with GTGEN_LLM_MODE=mock, no network calls."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from gtgen.data.loaders import load_settings
from gtgen.llm import LLMClient, LLMValidationError


# ---------------------------------------------------------------- test models

class SampleResponse(BaseModel):
    greeting: str
    count: int


class StrictResponse(BaseModel):
    """Model that won't match the bad fixture."""
    greeting: str
    count: int


# ---------------------------------------------------------------- helpers

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_REAL_HTTP_RETRY = load_settings(PROJECT_ROOT / "config/settings.yaml")["llm"]["http_retry"]

SETTINGS: dict = {
    "llm": {
        "model": "google/gemma-4-26b-a4b-it:free",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "api_key_roles": {
            "prep": "OPENROUTER_API_KEY_PREP", "claims": "OPENROUTER_API_KEY_CLAIMS",
            "persona": "OPENROUTER_API_KEY_PERSONA", "buyer": "OPENROUTER_API_KEY_BUYER",
            "negotiator": "OPENROUTER_API_KEY_NEGOTIATOR",
        },
        "backend": "openrouter",
        "backends": {
            "openrouter": {
                "provider": {"order": ["google-ai-studio"], "allow_fallbacks": False}
            },
            "gemini_direct": {
                "provider": {
                    "order": ["deepinfra"],
                    "allow_fallbacks": False,
                    "require_parameters": True,
                }
            },
        },
        "token_limit_total": 16000,
        "max_tokens_per_call": 15000,
        "max_completion_tokens": 6000,
        "min_completion_tokens": 1000,
        "max_retries": 2,
        "timeout_s": 120,
        "rate_limit": {"requests_per_minute": 20, "min_interval_s": 3.2},
        # the real schedule (config/settings.yaml, user decision 2026-10-31): 6 retries, linear 8 s steps
        "http_retry": _REAL_HTTP_RETRY,
        "temperature": {},
    },
    "paths": {"llm_cache": ""},  # overridden per test via tmp_path
}


class FakeClock:
    """Fake monotonic clock; sleeping advances it and records the durations."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Process-wide limiter driven by a fake clock: no test ever really sleeps."""
    from gtgen.llm import client as client_mod

    fc = FakeClock()
    monkeypatch.setattr(client_mod._LIMITER, "clock", fc.now)
    monkeypatch.setattr(client_mod._LIMITER, "sleep", fc.sleep)
    client_mod._LIMITER.reset()
    return fc


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force mock mode for every test."""
    monkeypatch.setenv("GTGEN_LLM_MODE", "mock")
    monkeypatch.delenv("GTGEN_LLM_BACKEND", raising=False)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LLMClient:
    """Create a client whose cache writes go to a temp directory."""
    # Point the fixture dir at the real fixtures directory
    monkeypatch.chdir(PROJECT_ROOT)
    return LLMClient(SETTINGS, cache_dir=str(tmp_path / "cache"))


MESSAGES = [{"role": "user", "content": "Say hello."}]


def _expected_id(prompt_name: str, version: int, backend: str = "openrouter", seed=None) -> str:
    return LLMClient._cache_key(
        "google/gemma-4-26b-a4b-it:free", prompt_name, version, MESSAGES, None, seed,
        backend=backend, provider=SETTINGS["llm"]["backends"][backend]["provider"],
    )


# ---------------------------------------------------------------- tests

def test_mock_mode_returns_fixture(client: LLMClient) -> None:
    """Mock mode loads the fixture and returns a valid parsed response."""
    result, call_id = client.call(
        prompt_name="test_prompt",
        prompt_version=1,
        messages=MESSAGES,
        response_model=SampleResponse,
    )
    assert isinstance(result, SampleResponse)
    assert result.greeting == "hello"
    assert result.count == 42
    assert call_id == _expected_id("test_prompt", 1)


def test_cache_hit(client: LLMClient, tmp_path: Path) -> None:
    """Second call with identical inputs hits the disk cache."""
    # First call — populates cache from fixture
    result1, id1 = client.call(
        prompt_name="test_prompt",
        prompt_version=1,
        messages=MESSAGES,
        response_model=SampleResponse,
    )
    # Verify cache file was written
    cache_dir = tmp_path / "cache"
    cache_files = list(cache_dir.rglob("*.json"))
    assert len(cache_files) == 1

    # Second call — same inputs, should hit cache (not fixture)
    result2, id2 = client.call(
        prompt_name="test_prompt",
        prompt_version=1,
        messages=MESSAGES,
        response_model=SampleResponse,
    )
    assert result2.greeting == result1.greeting
    assert result2.count == result1.count
    # llm_call_id is preserved from the cache entry
    assert id2 == id1


def test_cache_miss_different_inputs(client: LLMClient, tmp_path: Path) -> None:
    """Different inputs produce different cache keys, so a new fixture load occurs."""
    key_a = LLMClient._cache_key(
        "google/gemma-4-26b-a4b-it:free", "test_prompt", 1, MESSAGES, None, None
    )
    key_b = LLMClient._cache_key(
        "google/gemma-4-26b-a4b-it:free", "test_prompt", 1,
        [{"role": "user", "content": "Different input."}], None, None,
    )
    assert key_a != key_b


def test_llm_call_id_is_cache_key(client: LLMClient) -> None:
    """llm_call_id is the 64-hex sha256 cache key, never a uuid."""
    _, call_id = client.call(
        prompt_name="test_prompt", prompt_version=1,
        messages=MESSAGES, response_model=SampleResponse,
    )
    assert call_id == _expected_id("test_prompt", 1)
    assert len(call_id) == 64 and "-" not in call_id


def test_warm_cache_identical_id_and_bytes(client: LLMClient, tmp_path: Path) -> None:
    r1, id1 = client.call("test_prompt", 1, MESSAGES, SampleResponse)
    files = list((tmp_path / "cache").rglob("*.json"))
    assert len(files) == 1 and files[0].stem == id1
    bytes1 = files[0].read_bytes()
    r2, id2 = client.call("test_prompt", 1, MESSAGES, SampleResponse)
    assert id1 == id2
    assert r1 == r2 and r1.model_dump_json() == r2.model_dump_json()
    assert files[0].read_bytes() == bytes1  # warm hit does not rewrite
    # a fresh client over the same cache dir also hits and agrees
    r3, id3 = LLMClient(SETTINGS, cache_dir=str(tmp_path / "cache")).call(
        "test_prompt", 1, MESSAGES, SampleResponse
    )
    assert id3 == id1 and r3 == r1 and files[0].read_bytes() == bytes1


def test_backend_and_seed_change_id() -> None:
    base = _expected_id("p", 1)
    assert _expected_id("p", 1, backend="gemini_direct") != base
    assert _expected_id("p", 1, seed=1) != base
    assert _expected_id("p", 1, seed=1) != _expected_id("p", 1, seed=2)
    # provider block alone also changes the id
    k = LLMClient._cache_key("m", "p", 1, MESSAGES, None, None, backend="b", provider={"a": 1})
    k2 = LLMClient._cache_key("m", "p", 1, MESSAGES, None, None, backend="b", provider={"a": 2})
    assert k != k2


def test_backend_changes_id_via_clients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GTGEN_LLM_BACKEND", "gemini_direct")
    c = LLMClient(SETTINGS, cache_dir=str(tmp_path / "c"))
    with pytest.raises(FileNotFoundError, match="gemini_direct|test_prompt") as ei:
        c.call("test_prompt", 1, MESSAGES, SampleResponse)
    assert _expected_id("test_prompt", 1, backend="gemini_direct") in str(ei.value)


def test_cache_key_deterministic() -> None:
    """Same inputs always produce the same cache key."""
    args = ("google/gemma-4-26b-a4b-it:free", "prompt_x", 2, MESSAGES, {"k": "v"}, 42)
    key1 = LLMClient._cache_key(*args)
    key2 = LLMClient._cache_key(*args)
    assert key1 == key2
    # SHA-256 hex digest is 64 characters
    assert len(key1) == 64


def test_validation_error_raised(client: LLMClient) -> None:
    """When the fixture response doesn't match the model, LLMValidationError is raised."""
    with pytest.raises(LLMValidationError) as exc_info:
        client.call(
            prompt_name="bad_prompt",
            prompt_version=1,
            messages=MESSAGES,
            response_model=StrictResponse,
        )
    err = exc_info.value
    assert err.prompt_name == "bad_prompt"
    assert err.prompt_version == 1
    assert len(err.errors) > 0
    assert isinstance(err.raw_content, str)


def test_missing_fixture_raises(client: LLMClient) -> None:
    """Requesting a nonexistent fixture in mock mode raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="Mock fixture not found") as ei:
        client.call(
            prompt_name="nonexistent_prompt",
            prompt_version=99,
            messages=MESSAGES,
            response_model=SampleResponse,
        )
    assert "tests/fixtures/llm/nonexistent_prompt/" in str(ei.value)


def test_cache_key_seed_matters() -> None:
    """Different seeds produce different cache keys."""
    key_none = LLMClient._cache_key(
        "m", "p", 1, MESSAGES, None, None
    )
    key_42 = LLMClient._cache_key(
        "m", "p", 1, MESSAGES, None, 42
    )
    key_99 = LLMClient._cache_key(
        "m", "p", 1, MESSAGES, None, 99
    )
    assert key_none != key_42
    assert key_42 != key_99


def test_invalid_mode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalid GTGEN_LLM_MODE value raises ValueError at construction."""
    monkeypatch.setenv("GTGEN_LLM_MODE", "bogus")
    with pytest.raises(ValueError, match="must be one of"):
        LLMClient(SETTINGS)


# ---------------------------------------------------------------- retry / backoff

import httpx  # noqa: E402


def _live_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LLMClient:
    monkeypatch.setenv("GTGEN_LLM_MODE", "live")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-secret")
    c = LLMClient(SETTINGS, cache_dir=str(tmp_path / "cache"))
    return c


def test_validation_retry_then_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First response invalid; retry appends error feedback and succeeds."""
    c = _live_client(tmp_path, monkeypatch)
    seen: list[list[dict]] = []

    def fake_api(messages, temperature, seed, model):
        seen.append(list(messages))
        return '{"greeting": "hi", "count": 3}'

    monkeypatch.setattr(c, "_api_call", fake_api)
    parsed, errors = c._validate_with_retries(
        '{"greeting": "hi"}', MESSAGES, 0.1, 1, SampleResponse, "p", 1
    )
    assert parsed.count == 3 and errors == []
    assert len(seen) == 1
    assert seen[0][-2]["role"] == "assistant"
    assert "failed JSON schema validation" in seen[0][-1]["content"]


def test_validation_retry_then_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Always-invalid output: exactly max_retries re-calls, then LLMValidationError."""
    c = _live_client(tmp_path, monkeypatch)
    calls: list[int] = []

    def fake_api(messages, temperature, seed, model):
        calls.append(len(messages))
        return '{"greeting": 1}'

    monkeypatch.setattr(c, "_api_call", fake_api)
    with pytest.raises(LLMValidationError) as ei:
        c._validate_with_retries(
            "not json", MESSAGES, 0.1, 1, SampleResponse, "p", 1
        )
    assert len(calls) == 2  # max_retries
    assert calls[1] == calls[0] == 3  # base + latest rejected reply + correction (bounded, not accumulated)
    assert ei.value.prompt_name == "p" and ei.value.errors


def test_retry_attempt_bypasses_the_cached_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry with the same messages must not replay the cached answer that just failed the runner check."""
    c = _live_client(tmp_path, monkeypatch)
    calls: list[int] = []

    def fake_api(messages, temperature, seed, model):
        calls.append(len(messages))
        return '{"greeting": "hi", "count": 3}'

    monkeypatch.setattr(c, "_api_call", fake_api)
    _, id1 = c.call("p", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1, extra_inputs={"a": 1})
    _, id2 = c.call("p", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1, extra_inputs={"a": 1})
    _, id3 = c.call("p", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1,
                    extra_inputs={"a": 1, "_retry_attempt": 1})
    assert len(calls) == 2 and id2 == id1 and id3 != id1


class _Resp:
    def __init__(self, status: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = text
        self.headers: dict = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("POST", "https://x")
            raise httpx.HTTPStatusError(
                "err", request=req, response=httpx.Response(self.status_code, text=self.text)
            )

    def json(self) -> dict:
        return self._payload


_OK = {"choices": [{"message": {"content": '{"greeting":"a","count":1}'}}]}


def _post_seq(monkeypatch, responses, sent=None):
    it = iter(responses)

    def post(url, json=None, headers=None, timeout=None):
        if sent is not None:
            sent.append(json)
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(httpx, "post", post)


def test_backoff_uses_settings_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    c = _live_client(tmp_path, monkeypatch)
    _post_seq(monkeypatch, [_Resp(429), _Resp(503), _Resp(200, _OK)])
    assert "greeting" in c._api_call(MESSAGES, 0.1, 1, SampleResponse)
    # backoff waits 8 then 16; pacing adds nothing extra because the waits exceed 3.2
    assert fake_clock.sleeps == [8.0, 16.0]


def test_real_settings_schedule_is_linear_8s_six_retries() -> None:
    assert _REAL_HTTP_RETRY["max_attempts"] == 6
    assert [float(x) for x in _REAL_HTTP_RETRY["backoff_s"]] == [8, 16, 24, 32, 40, 48]
    assert _REAL_HTTP_RETRY["max_retry_after_s"] == 300 and _REAL_HTTP_RETRY["respect_retry_after"] is True


def test_429_gives_up_after_max_attempts_and_key_not_leaked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    fake_clock: FakeClock,
) -> None:
    from gtgen.llm import LLMNetworkError

    c = _live_client(tmp_path, monkeypatch)
    n = {"calls": 0}

    def post(*a, **k):
        n["calls"] += 1
        return _Resp(429, text="boom sk-test-secret")

    monkeypatch.setattr(httpx, "post", post)
    with caplog.at_level("DEBUG"):
        with pytest.raises(LLMNetworkError) as ei:
            c._api_call(MESSAGES, 0.1, 1, SampleResponse)
    assert n["calls"] == 7  # 1 + max_attempts(6) retries; the 7th failure raises
    assert fake_clock.sleeps == [8.0, 16.0, 24.0, 32.0, 40.0, 48.0]
    assert "sk-test-secret" not in str(ei.value)
    assert "sk-test-secret" not in caplog.text
    assert "waiting 8.0s" in caplog.text


def test_retry_after_seconds_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    c = _live_client(tmp_path, monkeypatch)
    r = _Resp(429)
    r.headers = {"Retry-After": "7"}
    _post_seq(monkeypatch, [r, _Resp(200, _OK)])
    assert "greeting" in c._api_call(MESSAGES, 0.1, 1, SampleResponse)
    assert fake_clock.sleeps == [12.0]   # D067: max(linear 8, 7 + 5 margin)


def test_retry_after_http_date_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    c = _live_client(tmp_path, monkeypatch)
    r = _Resp(429)
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    r.headers = {"retry-after": format_datetime(when, usegmt=True)}
    _post_seq(monkeypatch, [r, _Resp(200, _OK)])
    c._api_call(MESSAGES, 0.1, 1, SampleResponse)
    assert len(fake_clock.sleeps) == 1 and 30 <= fake_clock.sleeps[0] <= 36


def test_retry_after_too_long_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    from gtgen.llm import LLMNetworkError

    c = _live_client(tmp_path, monkeypatch)
    r = _Resp(429, text="quota")
    r.headers = {"Retry-After": "3600"}
    sent: list = []
    _post_seq(monkeypatch, [r, _Resp(200, _OK)], sent)
    with pytest.raises(LLMNetworkError, match="quota|rate limit"):
        c._api_call(MESSAGES, 0.1, 1, SampleResponse)
    assert len(sent) == 1 and fake_clock.sleeps == []


def test_5xx_and_timeout_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    c = _live_client(tmp_path, monkeypatch)
    _post_seq(
        monkeypatch,
        [httpx.ReadTimeout("t"), httpx.ConnectError("c"), _Resp(502), _Resp(200, _OK)],
    )
    c._api_call(MESSAGES, 0.1, 1, SampleResponse)
    assert fake_clock.sleeps == [8.0, 16.0, 24.0]


@pytest.mark.parametrize("status", [400, 401])
def test_no_retry_on_4xx(
    status: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    from gtgen.llm import LLMNetworkError

    c = _live_client(tmp_path, monkeypatch)
    sent: list = []
    _post_seq(monkeypatch, [_Resp(status, text="bad")], sent)
    with pytest.raises(LLMNetworkError):
        c._api_call(MESSAGES, 0.1, 1, SampleResponse)
    assert len(sent) == 1 and fake_clock.sleeps == []


# ---------------------------------------------------------------- pacing (D024)

def test_live_calls_are_spaced_and_cache_hit_does_not_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    c = _live_client(tmp_path, monkeypatch)
    starts: list[float] = []

    def post(url, json=None, headers=None, timeout=None):
        starts.append(fake_clock.now())
        return _Resp(200, _OK)

    monkeypatch.setattr(httpx, "post", post)
    ids = [
        c.call(f"p{i}", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)[1]
        for i in range(3)
    ]
    assert len(set(ids)) == 3
    assert all(b - a >= 3.2 - 1e-9 for a, b in zip(starts, starts[1:]))
    sleeps_before, t_before, n_before = list(fake_clock.sleeps), fake_clock.now(), len(starts)
    _, again = c.call("p0", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    assert again == ids[0]
    assert len(starts) == n_before and fake_clock.sleeps == sleeps_before
    assert fake_clock.now() == t_before


def test_limiter_shared_across_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    c1 = _live_client(tmp_path, monkeypatch)
    c2 = LLMClient(SETTINGS, cache_dir=str(tmp_path / "c2"))
    starts: list[float] = []
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: (starts.append(fake_clock.now()), _Resp(200, _OK))[1],
    )
    c1._api_call(MESSAGES, 0.1, 1, SampleResponse)
    c2._api_call(MESSAGES, 0.1, 1, SampleResponse)
    assert starts[1] - starts[0] >= 3.2 - 1e-9


def test_mock_mode_never_waits(client: LLMClient, fake_clock: FakeClock) -> None:
    client.call("test_prompt", 1, MESSAGES, SampleResponse, temperature=0.0)
    assert fake_clock.sleeps == []


def test_validation_retries_are_paced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    c = _live_client(tmp_path, monkeypatch)
    starts: list[float] = []
    bodies = iter(['{"greeting": 1}', '{"greeting": 1}', '{"greeting":"a","count":1}'])

    def post(url, json=None, headers=None, timeout=None):
        starts.append(fake_clock.now())
        return _Resp(200, {"choices": [{"message": {"content": next(bodies)}}]})

    monkeypatch.setattr(httpx, "post", post)
    parsed, _ = c.call("pv", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    assert parsed.count == 1 and len(starts) == 3
    assert all(b - a >= 3.2 - 1e-9 for a, b in zip(starts, starts[1:]))


# ---------------------------------------------------------------- token budget (D024)

def test_budget_shrinks_max_tokens_for_large_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _live_client(tmp_path, monkeypatch)
    sent: list = []
    _post_seq(monkeypatch, [_Resp(200, _OK), _Resp(200, _OK)], sent)
    c._api_call(MESSAGES, 0.1, 1, SampleResponse)
    assert sent[0]["max_tokens"] == 6000
    big = [{"role": "user", "content": "x" * 36000}]  # ~10300 est. tokens (prose ratio 3.5)
    c._api_call(big, 0.1, 1, SampleResponse)
    est = c._input_estimate(big, SampleResponse)
    assert 1000 <= sent[1]["max_tokens"] < 6000
    assert est + sent[1]["max_tokens"] <= 15000 and sent[1]["max_tokens"] == 15000 - est


def test_oversized_prompt_raises_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    from gtgen.llm import LLMBudgetError

    c = _live_client(tmp_path, monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("HTTP sent"))
    with pytest.raises(LLMBudgetError, match="too large"):
        c._api_call([{"role": "user", "content": "x" * 60000}], 0.1, 1, SampleResponse)
    assert fake_clock.sleeps == []


def test_budget_recomputed_on_validation_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _live_client(tmp_path, monkeypatch)
    sent: list = []
    bad = {"choices": [{"message": {"content": "y" * 8000}}]}  # invalid, ~2300 tokens echoed
    _post_seq(monkeypatch, [_Resp(200, bad), _Resp(200, _OK)], sent)
    msgs = [{"role": "user", "content": "x" * 16000}]  # ~4600 tokens
    c.call("rb", 1, msgs, SampleResponse, temperature=0.0, seed=1)
    assert sent[1]["max_tokens"] < sent[0]["max_tokens"]


# ---------------------------------------------------------------- fences (D024)

FENCED = '```json\n{\n  "greeting": "hi",\n  "count": 5\n}\n```'


def test_strip_code_fence_unit() -> None:
    from gtgen.llm.client import strip_code_fence

    assert strip_code_fence(FENCED).startswith("{")
    assert strip_code_fence('```\n{"a":1}\n```') == '{"a":1}'
    assert strip_code_fence('  ```json\n{"a":1}\n```  \n') == '{"a":1}'
    prose = 'Sure! Here you go:\n```json\n{"a":1}\n```'
    assert strip_code_fence(prose) == prose
    assert strip_code_fence('{"a":1}') == '{"a":1}'


def test_fenced_json_validates_and_cache_keeps_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _live_client(tmp_path, monkeypatch)
    _post_seq(monkeypatch, [_Resp(200, {"choices": [{"message": {"content": FENCED}}]})])
    parsed, call_id = c.call("fence", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    assert parsed.count == 5
    entry = json.loads(c._cache_path(call_id).read_text())
    assert entry["raw_content"] == FENCED
    assert entry["validated_text"].startswith("{") and "```" not in entry["validated_text"]


def test_first_json_object_ignores_trailing_prose() -> None:
    from gtgen.llm.client import first_json_object

    obj = '{"greeting": "hi", "count": 5}'
    assert first_json_object(obj) == (obj, "")
    assert first_json_object(f'{obj}\n```\nWait, let me reconsider.') == (
        obj, "```\nWait, let me reconsider."
    )
    assert first_json_object(f'```json\n{obj}\n```') == (obj, "")
    # Prose before the object is NOT accepted (D024); prompt_json has its own extractor.
    assert first_json_object(f'Sure: {obj}') is None


def test_trailing_prose_after_leading_json_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid leading object is accepted even if the model appends reasoning after it."""
    c = _live_client(tmp_path, monkeypatch)
    content = '{"greeting": "hi", "count": 5}\n```\nWait, let me reconsider.'
    sent: list = []
    _post_seq(monkeypatch, [_Resp(200, {"choices": [{"message": {"content": content}}]})], sent)
    parsed, _ = c.call("trailing", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    assert parsed.count == 5 and len(sent) == 1
    so = c.events[-1]["structured_output"]
    assert so["first_attempt_valid"] is True
    assert so["honoured"] is False and so["fallback"] == "trailing_text"


def test_prose_with_embedded_json_fails_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _live_client(tmp_path, monkeypatch)
    prose = 'Sure, here it is: {"greeting": "hi", "count": 5} hope that helps'
    sent: list = []
    _post_seq(
        monkeypatch,
        [_Resp(200, {"choices": [{"message": {"content": prose}}]})] * 3,
        sent,
    )
    with pytest.raises(LLMValidationError) as ei:
        c.call("prose", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    assert len(sent) == 3  # first try + max_retries
    assert ei.value.raw_content == prose


# ---------------------------------------------------------------- required settings (D024)

@pytest.mark.parametrize(
    "missing", ["rate_limit", "http_retry", "max_tokens_per_call", "min_completion_tokens"]
)
def test_missing_runtime_settings_raise(missing: str) -> None:
    llm = {k: v for k, v in SETTINGS["llm"].items() if k != missing}
    with pytest.raises(ValueError, match=missing):
        LLMClient({"llm": llm})


def test_runtime_limits_not_in_cache_key(tmp_path: Path) -> None:
    other = json.loads(json.dumps(SETTINGS))
    other["llm"]["rate_limit"]["min_interval_s"] = 9.9
    other["llm"]["max_completion_tokens"] = 1234
    a = LLMClient(SETTINGS, cache_dir=str(tmp_path / "a"))
    b = LLMClient(other, cache_dir=str(tmp_path / "b"))
    ka = a._cache_key(a._model, "p", 1, MESSAGES, None, 1, backend=a._backend, provider=a._provider)
    kb = b._cache_key(b._model, "p", 1, MESSAGES, None, 1, backend=b._backend, provider=b._provider)
    assert ka == kb


# ---------------------------------------------------------------- backends (D017)

def test_unknown_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GTGEN_LLM_BACKEND", "nope")
    with pytest.raises(ValueError, match="Unknown LLM backend"):
        LLMClient(SETTINGS)
    monkeypatch.delenv("GTGEN_LLM_BACKEND")
    bad = {"llm": {**SETTINGS["llm"], "backend": "zzz"}}
    with pytest.raises(ValueError, match="Unknown LLM backend"):
        LLMClient(bad)


@pytest.mark.parametrize("backend", ["openrouter", "gemini_direct"])
def test_request_body_provider_block(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GTGEN_LLM_MODE", "live")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-secret")
    monkeypatch.setenv("GTGEN_LLM_BACKEND", backend)
    c = LLMClient(SETTINGS, cache_dir=str(tmp_path / "c"))
    sent: dict = {}

    def post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, body=json, headers=headers)
        return _Resp(200, _OK)

    monkeypatch.setattr(httpx, "post", post)
    parsed, call_id = c.call("p", 1, MESSAGES, SampleResponse, temperature=0.0, seed=7)
    assert parsed.greeting == "a" and len(call_id) == 64
    assert sent["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert sent["body"]["provider"] == SETTINGS["llm"]["backends"][backend]["provider"]
    assert sent["body"]["response_format"]["type"] == "json_schema"
    assert sent["body"]["response_format"]["json_schema"]["strict"] is True
    assert sent["body"]["seed"] == 7 and sent["body"]["max_tokens"] <= 6000




# ---------------------------------------------------------------- D062: model / provider is :free via google-ai-studio only
import copy  # noqa: E402

import yaml  # noqa: E402

FREE = "google/gemma-4-26b-a4b-it:free"


def _settings_with(**llm_over) -> dict:
    s = copy.deepcopy(SETTINGS)
    s["llm"].update(llm_over)
    return s


def _real_settings() -> dict:
    return yaml.safe_load((PROJECT_ROOT / "config/settings.yaml").read_text(encoding="utf-8"))


def test_provider_block_from_settings_sent_as_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real settings.yaml model and provider block reach the request body unchanged (transport mocked)."""
    real = _real_settings()
    assert real["llm"]["model"] == FREE
    monkeypatch.setenv("GTGEN_LLM_MODE", "live")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-secret")
    monkeypatch.delenv("GTGEN_LLM_BACKEND", raising=False)
    c = LLMClient(real, cache_dir=str(tmp_path / "cache"))
    sent: list[dict] = []
    _post_seq(monkeypatch, [_Resp(200, _OK)], sent)
    c.call("p", 1, MESSAGES, StrictResponse, seed=1)
    body = sent[0]
    assert body["model"] == FREE
    assert body["provider"] == {"order": ["google-ai-studio"], "allow_fallbacks": False}
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["strict"] is True


def test_free_cache_entry_is_a_hit_for_the_configured_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry written under the :free id is a cache hit for the configured (:free) model: no HTTP, same id."""
    real = _real_settings()
    monkeypatch.setenv("GTGEN_LLM_MODE", "live")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-secret")
    monkeypatch.delenv("GTGEN_LLM_BACKEND", raising=False)
    cache = tmp_path / "cache"
    key = LLMClient._cache_key(FREE, "p", 1, MESSAGES, None, 3, backend="openrouter",
                               provider={"order": ["google-ai-studio"], "allow_fallbacks": False})
    path = cache / key[:2] / f"{key}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"response": {"greeting": "old", "count": 9}, "origin": "live"}))
    c = LLMClient(real, cache_dir=str(cache))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("HTTP sent on a cache hit"))
    r, cid = c.call("p", 1, MESSAGES, StrictResponse, seed=3)
    assert cid == key and r.greeting == "old" and c.events[-1]["source"] == "cache_hit"


def test_no_alternative_provider_anywhere() -> None:
    """Nothing paid or alternative-provider in src, tests, prompts, config (this test file builds the word at runtime)."""
    needle = "ver" + "tex"
    hits = []
    for top in ("src", "tests", "prompts", "config"):
        for f in (PROJECT_ROOT / top).rglob("*"):
            if f.is_file() and f.suffix in {".py", ".md", ".yaml", ".yml", ".json", ".txt", ".toml"} \
                    and ".cache" not in f.parts and "__pycache__" not in f.parts:
                if needle in f.read_text(encoding="utf-8", errors="ignore").lower():
                    hits.append(str(f.relative_to(PROJECT_ROOT)))
    assert hits == []


def test_fixture_identity_equals_configured_model() -> None:
    from gtgen.llm.client import FIXTURE_BACKEND, FIXTURE_MODEL, FIXTURE_PROVIDER

    real = _real_settings()["llm"]
    assert FIXTURE_MODEL == real["model"] == FREE
    assert FIXTURE_BACKEND == real["backend"]
    assert FIXTURE_PROVIDER == real["backends"][real["backend"]]["provider"]


# ---------------------------------------------------------------- D062: pacer (20 rpm, every outgoing request)

def test_pacer_never_exceeds_20_per_rolling_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    """25+ requests mixing fresh calls, validation retries and 429/5xx transport retries: any 20 consecutive
    request starts span >= 60 s on the fake clock, i.e. never more than 20 in a rolling minute."""
    c = _live_client(tmp_path, monkeypatch)
    starts: list[float] = []
    script = iter([
        _Resp(200, _OK), _Resp(429, text="slow"), _Resp(200, _OK),
        _Resp(200, {"choices": [{"message": {"content": '{"greeting": 1}'}}]}), _Resp(200, _OK),
        _Resp(503, text="x"), _Resp(200, _OK),
    ])

    def post(url, json=None, headers=None, timeout=None):
        starts.append(fake_clock.now())
        try:
            return next(script)
        except StopIteration:
            return _Resp(200, _OK)

    monkeypatch.setattr(httpx, "post", post)
    for i in range(24):
        c.call(f"pace{i}", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    assert len(starts) >= 25
    assert all(b - a >= 3.2 - 1e-9 for a, b in zip(starts, starts[1:]))
    for i in range(len(starts) - 20):
        assert starts[i + 20] - starts[i] >= 60.0, (i, starts[i + 20] - starts[i])
    # sliding 60 s window never holds more than 20 starts
    for i, t0 in enumerate(starts):
        assert sum(1 for t in starts[i:] if t < t0 + 60.0) <= 20


def test_limiter_is_process_global_module_object() -> None:
    from gtgen.llm import client as client_mod

    assert isinstance(client_mod._LIMITER, client_mod.LimiterPool)


# ---------------------------------------------------------------- D062: token estimation / budget

def test_estimator_is_conservative_for_json_and_non_ascii() -> None:
    from gtgen.llm.client import estimate_tokens

    js = json.dumps([{"idx": i, "unit": "mm", "value": 12.5} for i in range(20)], separators=(",", ":"))
    assert estimate_tokens(js) >= len(js) // 3
    assert estimate_tokens("hello world " * 10) >= (len("hello world " * 10)) // 4 + 1
    assert estimate_tokens("नमस्ते") == 6     # 1 token per non-ASCII char
    assert estimate_tokens("") == 0


def test_budget_total_never_exceeds_per_call_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _live_client(tmp_path, monkeypatch)
    for n in (100, 5000, 20000, 30000, 38000):
        msgs = [{"role": "user", "content": "a b " * (n // 4)}]
        try:
            mt = c._build_budget(msgs, SampleResponse)
        except Exception as exc:
            assert "too large" in str(exc)
            continue
        assert c._input_estimate(msgs, SampleResponse) + mt <= 15000
        assert mt == min(6000, 15000 - c._input_estimate(msgs, SampleResponse)) and mt >= 1000


def test_per_call_ceiling_cannot_exceed_endpoint_total() -> None:
    with pytest.raises(ValueError, match="token_limit_total"):
        LLMClient(_settings_with(max_tokens_per_call=17000, token_limit_total=16000))


def test_usage_recorded_next_to_estimate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _live_client(tmp_path, monkeypatch)
    payload = {**_OK, "usage": {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49}}
    _post_seq(monkeypatch, [_Resp(200, payload)])
    c.call("u", 1, MESSAGES, SampleResponse, seed=1, batch_info={"runner": "t", "batch_size": 1})
    ev = c.events[-1]
    assert ev["usage"] == {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49}
    assert ev["est_input_tokens"] > 0 and ev["max_tokens_sent"] == 6000
    assert ev["estimator_ratio"] == round(ev["est_input_tokens_all_requests"] / 40, 3)
    assert ev["batch"] == {"runner": "t", "batch_size": 1}
    assert ev["requests"][0]["usage"]["total_tokens"] == 49
    logged = [json.loads(x) for x in (tmp_path / "cache" / "provenance.jsonl").read_text().splitlines()]
    assert logged[-1]["usage"]["prompt_tokens"] == 40 and logged[-1]["structured_output"]["honoured"] is True


# ---------------------------------------------------------------- D062: structured output modes

def test_structured_default_is_auto_and_invalid_rejected() -> None:
    assert LLMClient(SETTINGS)._structured == "auto"
    with pytest.raises(ValueError, match="structured_output"):
        LLMClient(_settings_with(structured_output="nope"))


def test_prompt_json_mode_sends_no_response_format_and_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GTGEN_LLM_MODE", "live")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-secret")
    c = LLMClient(_settings_with(structured_output="prompt_json"), cache_dir=str(tmp_path / "cache"))
    sent: list[dict] = []
    fenced = {"choices": [{"message": {"content": 'Sure!\n```json\n{"greeting":"a","count":2}\n```'}}]}
    _post_seq(monkeypatch, [_Resp(200, fenced)], sent)
    parsed, cid = c.call("pj", 1, MESSAGES, SampleResponse, seed=1)
    assert parsed.count == 2
    assert "response_format" not in sent[0]
    assert "JSON Schema" in sent[0]["messages"][0]["content"] and '"greeting"' in sent[0]["messages"][0]["content"]
    assert MESSAGES[0]["content"] == "Say hello."            # caller's messages untouched
    so = c.events[-1]["structured_output"]
    assert so["effective"] == "prompt_json" and so["response_format_sent"] is False and so["first_attempt_valid"]
    # key differs from the json_schema key; json_schema/auto keys carry no new field
    base = LLMClient._cache_key(FREE, "pj", 1, MESSAGES, None, 1, backend="openrouter", provider=c._provider)
    assert cid != base
    assert cid == LLMClient._cache_key(FREE, "pj", 1, MESSAGES, None, 1, backend="openrouter",
                                       provider=c._provider, structured_output="prompt_json")


def test_prompt_json_validation_retry_then_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GTGEN_LLM_MODE", "live")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-secret")
    c = LLMClient(_settings_with(structured_output="prompt_json"), cache_dir=str(tmp_path / "cache"))
    bad = {"choices": [{"message": {"content": "no json at all"}}]}
    sent: list[dict] = []
    _post_seq(monkeypatch, [_Resp(200, bad)] * 3, sent)
    with pytest.raises(LLMValidationError):
        c.call("pj2", 1, MESSAGES, SampleResponse, seed=1)
    assert len(sent) == 3                                   # first try + max_retries (2)
    assert all("response_format" not in b for b in sent)
    assert c.failed_events[-1]["source"] == "failed"


def test_auto_mode_records_honoured_and_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _live_client(tmp_path, monkeypatch)
    sent: list[dict] = []
    fenced = {"choices": [{"message": {"content": FENCED}}]}
    _post_seq(monkeypatch, [_Resp(200, _OK), _Resp(200, fenced)], sent)
    c.call("a1", 1, MESSAGES, SampleResponse, seed=1)
    c.call("a2", 1, MESSAGES, SampleResponse, seed=1)
    assert "response_format" in sent[0] and "response_format" in sent[1]
    assert c.events[0]["structured_output"]["honoured"] is True
    so = c.events[1]["structured_output"]
    assert so["configured"] == "auto" and so["honoured"] is False and so["fallback"] == "code_fence"


def test_http_400_on_response_format_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gtgen.llm import LLMNetworkError

    c = _live_client(tmp_path, monkeypatch)
    _post_seq(monkeypatch, [_Resp(400, text="response_format not supported")])
    with pytest.raises(LLMNetworkError):
        c.call("a3", 1, MESSAGES, SampleResponse, seed=1)
    fe = c.failed_events[-1]
    assert fe["structured_output"]["honoured"] is False and fe["structured_output"]["fallback"].endswith(":400")
    assert c.events == []                                   # failures never count as calls in the summary
