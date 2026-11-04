"""D063: per-role API keys, per-key rate limiters, redaction, cache-key independence. No network."""
from __future__ import annotations

import copy
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from gtgen.data.loaders import load_settings
from gtgen.llm import LLMClient, LLMNetworkError
from gtgen.llm import client as client_mod
from gtgen.llm.client import PROMPT_ROLES, role_for_prompt
from gtgen.llm.prompts import PROMPT_NAMES

from .test_client import SETTINGS, FakeClock, fake_clock, _OK, _Resp, MESSAGES, SampleResponse  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
ROLE_VARS = {
    "prep": "OPENROUTER_API_KEY_PREP", "claims": "OPENROUTER_API_KEY_CLAIMS",
    "persona": "OPENROUTER_API_KEY_PERSONA", "buyer": "OPENROUTER_API_KEY_BUYER",
    "negotiator": "OPENROUTER_API_KEY_NEGOTIATOR",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ["OPENROUTER_API_KEY", *ROLE_VARS.values()]:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("GTGEN_LLM_MODE", "live")


def _client(tmp_path: Path) -> LLMClient:
    return LLMClient(SETTINGS, cache_dir=str(tmp_path / "cache"))


def test_every_prompt_name_resolves_to_a_known_role() -> None:
    for name in PROMPT_NAMES:
        assert role_for_prompt(name) in ROLE_VARS, f"prompt {name!r} has no API-key role (D063): add it to PROMPT_ROLES"
    assert {r for r in map(role_for_prompt, PROMPT_NAMES)} == set(ROLE_VARS)
    assert set(PROMPT_ROLES.values()) <= set(ROLE_VARS)
    assert role_for_prompt("negotiator_brand_new") == "negotiator"
    assert role_for_prompt("unknown_prompt") is None


def test_real_settings_define_every_role_variable() -> None:
    llm = load_settings(ROOT / "config/settings.yaml")["llm"]
    assert llm["api_key_roles"] == ROLE_VARS and llm["api_key_env"] == "OPENROUTER_API_KEY"


def test_real_settings_claims_pool_uses_every_role_variable() -> None:
    """D084: doc_claim batches may use all five accounts; every pool entry is a known role variable."""
    llm = load_settings(ROOT / "config/settings.yaml")["llm"]
    pool = llm.get("api_key_pools", {}).get("claims", [])
    assert set(pool) == set(ROLE_VARS.values())
    assert set(llm.get("api_key_pools", {})) <= set(ROLE_VARS)


def test_real_settings_dialogue_pools_use_every_role_variable() -> None:
    """D086: buyer/negotiator dialogue calls pool all five accounts to avoid single-key pacer waits."""
    llm = load_settings(ROOT / "config/settings.yaml")["llm"]
    pools = llm.get("api_key_pools", {})
    for role in ("buyer", "negotiator"):
        assert set(pools.get(role, [])) == set(ROLE_VARS.values())
    assert set(pools) <= set(ROLE_VARS)


def test_role_key_used_and_default_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-default")
    monkeypatch.setenv("OPENROUTER_API_KEY_BUYER", "sk-buyer")
    c = _client(tmp_path)
    c._prompt_name = "buyer_reply"
    assert c._resolve_api_key() == ("OPENROUTER_API_KEY_BUYER", "sk-buyer", "buyer")
    c._prompt_name = "persona"          # role var unset -> default
    assert c._resolve_api_key() == ("OPENROUTER_API_KEY", "sk-default", "persona")
    c._prompt_name = None               # direct _api_call without call(): default
    assert c._resolve_api_key()[0] == "OPENROUTER_API_KEY"


@pytest.mark.parametrize("val", ["", "   ", "sk-or-v1-REPLACE_ME_PREP"])
def test_empty_or_placeholder_role_key_is_unset(
    val: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-default")
    monkeypatch.setenv("OPENROUTER_API_KEY_PREP", val)
    c = _client(tmp_path)
    c._prompt_name = "reframe"
    assert c._resolve_api_key()[:2] == ("OPENROUTER_API_KEY", "sk-default")


def test_missing_everywhere_names_variable_not_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-REPLACE_ME_DEFAULT")
    c = _client(tmp_path)
    c._prompt_name = "claim_check"
    with pytest.raises(EnvironmentError) as ei:
        c._resolve_api_key()
    assert "OPENROUTER_API_KEY" in str(ei.value) and "REPLACE_ME" not in str(ei.value)


def _pool_client(tmp_path: Path, pool: list[str]) -> LLMClient:
    settings = copy.deepcopy(SETTINGS)
    settings["llm"]["api_key_pools"] = {"claims": pool}
    return LLMClient(settings, cache_dir=str(tmp_path / "cache"))


def test_key_pool_round_robins_only_usable_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY_CLAIMS", "sk-c")
    monkeypatch.setenv("OPENROUTER_API_KEY_PREP", "sk-p")
    c = _pool_client(
        tmp_path,
        ["OPENROUTER_API_KEY_CLAIMS", "OPENROUTER_API_KEY_PREP", "OPENROUTER_API_KEY_PERSONA"],
    )
    c._prompt_name = "doc_claim"
    seq = [c._resolve_api_key() for _ in range(5)]
    assert [name for name, _, _ in seq] == [
        "OPENROUTER_API_KEY_CLAIMS", "OPENROUTER_API_KEY_PREP",
        "OPENROUTER_API_KEY_CLAIMS", "OPENROUTER_API_KEY_PREP",
        "OPENROUTER_API_KEY_CLAIMS",
    ]
    assert {role for _, _, role in seq} == {"claims"}


def test_key_pool_falls_back_to_role_then_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY_CLAIMS", "sk-c")
    c = _pool_client(tmp_path, ["OPENROUTER_API_KEY_PREP"])
    c._prompt_name = "doc_claim"
    # Pool key unusable -> the role variable still wins over the default.
    assert c._resolve_api_key() == ("OPENROUTER_API_KEY_CLAIMS", "sk-c", "claims")
    monkeypatch.delenv("OPENROUTER_API_KEY_CLAIMS")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-d")
    # Neither pool nor role usable -> default.
    assert c._resolve_api_key() == ("OPENROUTER_API_KEY", "sk-d", "claims")


def test_pool_keys_are_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY_EXTRA", "sk-extra-SECRET")
    c = _pool_client(tmp_path, ["OPENROUTER_API_KEY_EXTRA"])
    assert "sk-extra-SECRET" not in c._redact("echo sk-extra-SECRET")


def test_pooled_calls_rotate_keys_and_use_separate_limiters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY_CLAIMS", "sk-c")
    monkeypatch.setenv("OPENROUTER_API_KEY_PREP", "sk-p")
    c = _pool_client(
        tmp_path, ["OPENROUTER_API_KEY_CLAIMS", "OPENROUTER_API_KEY_PREP"]
    )
    c._prompt_name = "doc_claim"
    auth: list[str] = []
    starts: list[float] = []

    def post(url, json=None, headers=None, timeout=None):
        auth.append(headers["Authorization"])
        starts.append(fake_clock.now())
        return _Resp(200, _OK)

    monkeypatch.setattr(httpx, "post", post)
    c._api_call(MESSAGES, 0.0, 1, SampleResponse)
    c._api_call(MESSAGES, 0.0, 1, SampleResponse)
    assert auth == ["Bearer sk-c", "Bearer sk-p"]
    # Different keys -> different limiters -> the second call is not paced behind the first.
    assert starts[1] == starts[0] and fake_clock.sleeps == []
    assert c._last_key["api_key_env"] == "OPENROUTER_API_KEY_PREP"


def _record_post(monkeypatch, store: list):
    def post(url, json=None, headers=None, timeout=None):
        store.append(headers["Authorization"])
        return _Resp(200, _OK)

    monkeypatch.setattr(httpx, "post", post)


def test_call_sends_role_key_and_logs_names_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-default-AAAA")
    monkeypatch.setenv("OPENROUTER_API_KEY_NEGOTIATOR", "sk-neg-BBBB")
    c = _client(tmp_path)
    auth: list[str] = []
    _record_post(monkeypatch, auth)
    c.call("negotiator_reply", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    c.call("reframe", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    assert auth == ["Bearer sk-neg-BBBB", "Bearer sk-default-AAAA"]
    ev = c.events
    assert (ev[0]["api_key_role"], ev[0]["api_key_env"]) == ("negotiator", "OPENROUTER_API_KEY_NEGOTIATOR")
    assert (ev[1]["api_key_role"], ev[1]["api_key_env"]) == ("prep", "OPENROUTER_API_KEY")
    logged = (tmp_path / "cache" / "provenance.jsonl").read_text()
    cached = "".join(p.read_text() for p in (tmp_path / "cache").rglob("*.json"))
    for secret in ("sk-default-AAAA", "sk-neg-BBBB", "sk-neg", "sk-def"):
        assert secret not in logged and secret not in cached


def test_llm_call_id_independent_of_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-d")
    monkeypatch.setenv("OPENROUTER_API_KEY_BUYER", "sk-b")
    monkeypatch.setenv("OPENROUTER_API_KEY_PREP", "sk-p")
    _record_post(monkeypatch, [])
    ids = []
    for cache, role_vars in (("c1", True), ("c2", False)):
        if not role_vars:
            monkeypatch.delenv("OPENROUTER_API_KEY_BUYER")
            monkeypatch.delenv("OPENROUTER_API_KEY_PREP")
        c = LLMClient(SETTINGS, cache_dir=str(tmp_path / cache))
        ids.append((c.call("buyer_reply", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)[1],
                    c.call("reframe", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)[1]))
    assert ids[0] == ids[1]


def test_limiters_independent_per_key_and_shared_for_same_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-d")
    monkeypatch.setenv("OPENROUTER_API_KEY_BUYER", "sk-b")
    monkeypatch.setenv("OPENROUTER_API_KEY_PREP", "sk-p")
    c = _client(tmp_path)
    starts: list[tuple[str, float]] = []

    def post(url, json=None, headers=None, timeout=None):
        starts.append((headers["Authorization"], fake_clock.now()))
        return _Resp(200, _OK)

    monkeypatch.setattr(httpx, "post", post)
    # distinct keys: no waiting between them
    for name in ("buyer_reply", "reframe", "persona"):     # buyer, prep, persona->default
        c.call(name, 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    assert len({t for _, t in starts}) == 1 and fake_clock.sleeps == []
    # same key again (persona shares the default key with e.g. negotiator_reply): paced by min_interval 3.2
    c.call("negotiator_reply", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    assert starts[-1][0] == "Bearer sk-d" and starts[-1][1] - starts[2][1] >= 3.2 - 1e-9
    # the buyer key has its own limiter: its second request is paced from its first one
    c.call("reveal_check", 1, MESSAGES, SampleResponse, temperature=0.0, seed=1)
    assert starts[-1][0] == "Bearer sk-b" and starts[-1][1] - starts[0][1] >= 3.2 - 1e-9


def test_retry_sleep_and_pacing_use_the_resolved_keys_limiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-d")
    monkeypatch.setenv("OPENROUTER_API_KEY_BUYER", "sk-b")
    c = _client(tmp_path)
    c._prompt_name = "buyer_reply"
    seq = iter([_Resp(503), _Resp(200, _OK)])
    monkeypatch.setattr(httpx, "post", lambda *a, **k: next(seq))
    c._api_call(MESSAGES, 0.0, 1, SampleResponse)
    assert fake_clock.sleeps == [8.0]


def test_redaction_scrubs_every_configured_key_in_raised_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    keys = {"OPENROUTER_API_KEY": "sk-default-111", **{v: f"sk-{r}-222" for r, v in ROLE_VARS.items()}}
    for k, v in keys.items():
        monkeypatch.setenv(k, v)
    c = _client(tmp_path)
    c._prompt_name = "persona"
    echoed = " | ".join(keys.values())
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(400, text=f"bad keys {echoed}"))
    with caplog.at_level("DEBUG"), pytest.raises(LLMNetworkError) as ei:
        c._api_call(MESSAGES, 0.0, 1, SampleResponse)
    for v in keys.values():
        assert v not in str(ei.value) and v not in caplog.text
    assert "[REDACTED]" in str(ei.value)
    # transport error text as well
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError(f"x {echoed}")))
    with pytest.raises(LLMNetworkError) as ei2:
        c._api_call(MESSAGES, 0.0, 1, SampleResponse)
    assert all(v not in str(ei2.value) for v in keys.values())
