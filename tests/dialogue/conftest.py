"""Fixtures for dialogue tests (definitions in helpers.py)."""
from dialogue.helpers import *  # noqa: F401,F403
from dialogue.helpers import (  # noqa: F401
    alignments, brief, catalogue, constraints, fake_runners, lenient_persona, pr_settings, profile, settings, strict_persona,
)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _negotiator_final_double(monkeypatch):
    """Until llm-engineer's runner lands (D069/D054), give the real LLMRunners a double with its exact signature."""
    from gtgen.llm import LLMResult, LLMRunners
    if hasattr(LLMRunners, "negotiator_final"):
        return

    def negotiator_final(self, kind, facts, sales_persona=None, seed=None, ctx=None):
        names = "; ".join(f"{o['product']} ({o['supplier']})" for o in facts["options"]) or "nothing"
        return LLMResult(f"[{kind}] Options: {names}.", ["id_final_double"])

    monkeypatch.setattr(LLMRunners, "negotiator_final", negotiator_final, raising=False)
