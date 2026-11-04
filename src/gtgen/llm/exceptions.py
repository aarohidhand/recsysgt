"""LLM client exceptions."""


class LLMValidationError(Exception):
    """Raised after max retries when structured output fails validation."""

    def __init__(
        self,
        prompt_name: str,
        prompt_version: int,
        errors: list,
        raw_content: str,
    ) -> None:
        self.prompt_name = prompt_name
        self.prompt_version = prompt_version
        self.errors = errors
        self.raw_content = raw_content
        super().__init__(
            f"LLM validation failed for {prompt_name}.v{prompt_version}: {errors}"
        )


class LLMNetworkError(Exception):
    """Raised when the OpenRouter API call fails at the network/HTTP level."""


class LLMBudgetError(Exception):
    """Raised before sending when the prompt leaves too little completion budget."""


class LLMCallFailed(RuntimeError):
    """A runner result had qa_flags and no value; raised by ``LLMResult.require()`` (D054: never a stub)."""


def bid_from_scenario_id(scenario_id: str) -> str:
    """Best-effort bid id from a ConversationContext.scenario_id ('<bid>:prep', '<bid>_s0', 'probe:<bid>')."""
    import re
    s = scenario_id or ""
    if s.startswith("probe:"):
        s = s[len("probe:"):]
    return re.sub(r"(:prep|_s\d+)$", "", s)


class ScenarioSkipped(Exception):
    """D066: a call used all its retries (validation or HTTP) and still failed; the caller skips this scenario/bid.

    Never carries a fabricated value.  ``bid_id`` / ``persona_seed`` come from the ConversationContext when given.
    """

    def __init__(self, prompt_name: str, last_error: str, ctx=None) -> None:
        self.prompt_name = prompt_name
        self.last_error = str(last_error)
        self.bid_id = bid_from_scenario_id(ctx.scenario_id) if ctx is not None else None
        self.persona_seed = ctx.cache_seed if ctx is not None else None
        self.scenario_id = ctx.scenario_id if ctx is not None else None
        super().__init__(str(self))

    def __str__(self) -> str:
        return (f"skip bid={self.bid_id} seed={self.persona_seed} prompt={self.prompt_name}: "
                f"{self.last_error}")
