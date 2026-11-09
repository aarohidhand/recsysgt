"""A bid with no candidate products is skipped before any LLM call. No network."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

_env = dict(os.environ)
from gtgen import cli  # noqa: E402  (importing cli loads the real .env into os.environ)
os.environ.clear()
os.environ.update(_env)  # keep real keys and GTGEN_LLM_BACKEND out of every other test
from gtgen.data import loaders  # noqa: E402
from gtgen.llm import ScenarioSkipped  # noqa: E402


class _NoLLM:
    client = SimpleNamespace(strict=False)

    def __getattr__(self, name):
        raise AssertionError(f"LLM step {name!r} called for a bid without candidates")


def test_bid_without_candidates_skips_before_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loaders, "build_scenario_brief", lambda *a, **k: SimpleNamespace(constraints=[object()]))
    monkeypatch.setattr(loaders, "build_catalogue_view", lambda *a, **k: SimpleNamespace(candidates=[]))
    with pytest.raises(ScenarioSkipped) as exc:
        cli.prepare_bid("GEM/2024/B/1", {"paths": {"raw": "unused"}}, _NoLLM())
    assert exc.value.prompt_name == "catalogue"
    assert "no candidate products" in exc.value.last_error
