"""D050 amendment: the public WRAP_UP turn.

Patience and the turn cap are hidden buyer parameters, so a negotiator can only learn that it must give its final offer
from the transcript. When they run out, the loop appends ONE buyer turn with exactly ``WRAP_UP_TEXT`` (no LLM) and calls
``step()`` once more. ``is_wrap_up`` is the single documented way to recognise that turn; the GT negotiator uses it,
a system under test may use it or its own reading of the text.
"""
from __future__ import annotations

import re

WRAP_UP_TEXT = "I have to wrap up now. What is the best you can offer from what we have discussed?"

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


_MARKER = _norm("I have to wrap up now")


def is_wrap_up(utterance: str) -> bool:
    """True iff ``utterance`` is (or contains, case- and whitespace-insensitively) the wrap-up request."""
    n = _norm(utterance)
    return _MARKER in n or _norm(WRAP_UP_TEXT) in n
