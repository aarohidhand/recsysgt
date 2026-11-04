"""Per-conversation context (D054) and the live-mode no-template guard."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional



class LiveModeTemplateError(RuntimeError):
    """A stub, template or canned fallback was used where live mode requires a model-generated text (D054)."""


class MissingConversationContext(ValueError):
    """Live mode requires a ConversationContext on every runner call (cache-key separation, D054)."""


@dataclass(frozen=True)
class ConversationContext:
    """Identifies one call inside one conversation. It is part of the cache key.

    ``turn_idx`` is None for pre-dialogue calls (reframe, persona, align, ...). ``cache_seed`` is the buyer's real
    ``persona.persona_seed`` on the buyer side, but on the negotiator side it is ``loop.negotiator_cache_seed``'s
    ONE-WAY DERIVED value (D061, W3: the real persona seed must never reach the negotiator). The field used to be
    named ``persona_seed`` even when it held that derived value, which leaked the name into every negotiator-call
    repr the barrier scan looks at; renamed here (D076 cleanup) so the name itself never implies the real seed.
    """
    scenario_id: str
    cache_seed: int
    turn_idx: Optional[int] = None
    # D073: provenance linkage only (separates runs in .cache/llm/provenance.jsonl); NOT part of the cache key.
    run_id: Optional[str] = None

    def as_dict(self) -> dict:
        # the dict key stays "persona_seed": it is part of every existing cache key's JSON payload, and changing
        # the key string (not just the attribute name) would invalidate every cache entry ever written.
        d = {"scenario_id": self.scenario_id, "turn_idx": self.turn_idx, "persona_seed": self.cache_seed}
        if self.run_id:
            d["run_id"] = self.run_id
        return d

    def at_turn(self, turn_idx: int) -> "ConversationContext":
        return ConversationContext(self.scenario_id, self.cache_seed, turn_idx, self.run_id)


def forbid_template_in_live(client: Any, what: str) -> None:
    """Call this wherever code is about to use a stub / template / canned text instead of a model answer.

    Raises ``LiveModeTemplateError`` when ``client`` is live (or record); mock-mode clients and test doubles
    (objects without ``is_live``) pass.
    """
    if getattr(client, "is_live", False):
        raise LiveModeTemplateError(
            f"{what}: stubs, templates and canned fallbacks are forbidden in live mode (D054); "
            "surface the failed call as an error / qa_flag instead")


# ------------------------------------------------------------------ number discipline
_NUM = re.compile(r"(?<![A-Za-z0-9_.])\d[\d,]*(?:\.\d+)?|(?<![A-Za-z0-9_])\.\d+")


def numbers_in(text: Any) -> set[float]:
    """All numeric tokens of ``text`` as floats ("1,50,000" -> 150000.0, "1.5" -> 1.5)."""
    out: set[float] = set()
    for m in _NUM.finditer(str(text)):
        try:
            out.add(float(m.group(0).replace(",", "")))
        except ValueError:
            continue
    return out


def numbers_problem(utterance: str, allowed_sources: Iterable[Any]) -> Optional[str]:
    """Error text if ``utterance`` states a number that occurs in none of ``allowed_sources``."""
    allowed: set[float] = set()
    for a in allowed_sources:
        if isinstance(a, (int, float)) and not isinstance(a, bool):
            allowed.add(float(a))
        elif a is not None:
            allowed |= numbers_in(a)
    bad = sorted(n for n in numbers_in(utterance) if n not in allowed)
    if bad:
        shown = ", ".join(f"{b:g}" for b in bad)
        return (f"the text states number(s) {shown} that are not in the allowed numbers; "
                "use only the numbers you were given, or no number")
    return None
