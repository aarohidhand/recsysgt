"""Progressive reveal (D044): settings, opening selection, reveal order and the touched-constraint matcher.

Everything here is pure, deterministic code over buyer-side data (the Buyer owns it; the Negotiator never imports
this module's selection functions). All ``dialogue.progressive_reveal`` keys are REQUIRED: a missing key raises
``KeyError`` (no in-code fallback).

Priority order (opening and later batches alike), a stable sort on
    (negotiated before verification_only (D044 amendment 3), HARD first, constraint_type in key_spec_types first, index in category_priority with unknown categories last,
     original order).
Constraints whose ``framing_decision`` is DROP / MERGE or whose role is DROPPED are never candidates.

Touched-constraint matcher (``touched``): an unrevealed constraint is touched by a negotiator utterance U when
    tokens(U) = lower-case alphanumeric words of U, each lightly stemmed (a trailing plural "s" is removed from
    words longer than 3 letters that do not end in "ss");
    STOP = fixed list of generic words (units of time, "value", "minimum", "supplier", "order", ...) plus the
    stemmed tokens of the constraint's own ``unit``;
    for each name in (attribute, spec_name): N = stemmed tokens of the name minus STOP;
        rule 1: N is non-empty and |N & tokens(U)| / |N| >= 0.5;
    rule 2: some "distinctive" token of ``commercial_text`` occurs in U; distinctive = stemmed alphabetic token of
        at least 5 letters that is not in STOP.
Rule 1 catches attribute wording ("certification" for ``iso_certification``, 1 of 2 tokens); rule 2 catches
paraphrase through the commercial wording ("warranty" for "At least 24 months of warranty").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

from gtgen.contracts import BuyerConstraint, FramingDecision, Hardness, ScenarioRole

from ._util import overlap_ratio

_WORD = re.compile(r"[a-z0-9]+")

STOP = frozenset({
    "day", "week", "month", "year", "hour", "minute", "unit", "piece", "pcs", "number", "value", "type", "name",
    "min", "max", "minimum", "maximum", "least", "most", "within", "about", "around", "least", "with", "without",
    "have", "should", "would", "could", "please", "ideally", "preferably", "required", "require", "need",
    "supplier", "product", "company", "order", "item", "must", "that", "this", "your", "from", "which", "their",
    "there", "other", "certified", "manufacturer", "supply", "provide", "available", "availability",
})


@dataclass(frozen=True)
class RevealConfig:
    enabled: bool
    opening_min: int
    opening_max: int
    key_spec_types: tuple[str, ...]
    category_priority: tuple[str, ...]
    clarify_max_rounds: int
    reveal_max_per_answer: int
    clarify_max_rounds_complex: int = 0       # D072: absolute maximum, only for a complex brief
    clarify_complex_min_stated: int = 0       # D072: complex = the opening states at least this many constraints

    def clarify_rounds_allowed(self, n_stated: int) -> int:
        """D072: ``clarify_max_rounds``, or ``clarify_max_rounds_complex`` when the opening states >= clarify_complex_min_stated."""
        return self.clarify_max_rounds_complex if n_stated >= self.clarify_complex_min_stated else self.clarify_max_rounds


def reveal_config(settings: dict) -> RevealConfig:
    """Read ``dialogue.progressive_reveal``. Every key is required; a missing key raises ``KeyError``."""
    pr = settings["dialogue"]["progressive_reveal"]
    cfg = RevealConfig(
        enabled=bool(pr["enabled"]), opening_min=int(pr["opening_min"]), opening_max=int(pr["opening_max"]),
        key_spec_types=tuple(str(x) for x in pr["key_spec_types"]),
        category_priority=tuple(str(x).upper() for x in pr["category_priority"]),
        clarify_max_rounds=int(pr["clarify_max_rounds"]), reveal_max_per_answer=int(pr["reveal_max_per_answer"]),
        clarify_max_rounds_complex=int(pr["clarify_max_rounds_complex"]),
        clarify_complex_min_stated=int(pr["clarify_complex_min_stated"]))
    if cfg.opening_min < 1 or cfg.opening_max < cfg.opening_min:
        raise ValueError("progressive_reveal: need 1 <= opening_min <= opening_max")
    if cfg.clarify_max_rounds < 0 or cfg.reveal_max_per_answer < 1:
        raise ValueError("progressive_reveal: clarify_max_rounds >= 0 and reveal_max_per_answer >= 1 required")
    if cfg.clarify_max_rounds_complex < cfg.clarify_max_rounds or cfg.clarify_complex_min_stated < 1:
        raise ValueError("progressive_reveal: clarify_max_rounds_complex >= clarify_max_rounds and clarify_complex_min_stated >= 1 required")
    return cfg


def _cfg(settings: "dict | RevealConfig") -> RevealConfig:
    return settings if isinstance(settings, RevealConfig) else reveal_config(settings)


def reveal_candidates(constraints: Sequence[BuyerConstraint]) -> list[BuyerConstraint]:
    """Constraints that may ever be said: everything except framing_decision DROP / MERGE and role DROPPED."""
    return [c for c in constraints
            if c.framing_decision not in (FramingDecision.DROP, FramingDecision.MERGE)
            and c.scenario_role != ScenarioRole.DROPPED]


def priority_order(constraints: Sequence[BuyerConstraint], settings: "dict | RevealConfig"
                   ) -> list[BuyerConstraint]:
    """Stable priority order (see module docstring). DROP / MERGE / DROPPED constraints are excluded.

    D044 amendment 3: scenario role first (NEGOTIATED, or None, before VERIFICATION_ONLY); verification-only
    constraints stay in the queue and are voiced later."""
    cfg = _cfg(settings)
    cands = reveal_candidates(constraints)

    def key(item: tuple[int, BuyerConstraint]) -> tuple[int, int, int, int, int]:
        i, c = item
        cat = (c.constraint_category or "").upper()
        cat_idx = cfg.category_priority.index(cat) if cat in cfg.category_priority else len(cfg.category_priority)
        return (1 if c.scenario_role == ScenarioRole.VERIFICATION_ONLY else 0,
                0 if c.hardness == Hardness.HARD else 1,
                0 if (c.constraint_type or "") in cfg.key_spec_types else 1, cat_idx, i)

    return [c for _, c in sorted(enumerate(cands), key=key)]


def same_area(a: BuyerConstraint, b: BuyerConstraint) -> bool:
    """Two constraints that restate one requirement in different words ("inside compartments" / "inside zippered
    compartments"): stemmed attribute-name Dice overlap > 0.5 (so "cert_1" and "cert_2" differ; or an identical commercial wording)."""
    if overlap_ratio(a.attribute, b.attribute) > 0.5:
        return True
    return bool(a.commercial_text) and a.commercial_text == b.commercial_text


def dedupe_areas(ordered: Sequence[BuyerConstraint], limit: Optional[int] = None) -> list[BuyerConstraint]:
    """First-come order, skipping a constraint that is in the same area as one already kept (it stays unrevealed and
    is voiced in a later answer). ``limit`` stops after that many kept constraints (D044/D065 caps)."""
    kept: list[BuyerConstraint] = []
    for c in ordered:
        if limit is not None and len(kept) >= limit:
            break
        if not any(same_area(c, k) for k in kept):
            kept.append(c)
    return kept


def opening_size(persona_seed: int, n_candidates: int, settings: "dict | RevealConfig") -> int:
    cfg = _cfg(settings)
    n = cfg.opening_min + persona_seed % (cfg.opening_max - cfg.opening_min + 1)
    return min(n, n_candidates)


def select_opening(constraints: Sequence[BuyerConstraint], persona_seed: int, settings: "dict | RevealConfig"
                   ) -> list[BuyerConstraint]:
    """The constraints the opening should state: first N of the priority order,
    N = opening_min + persona_seed % (opening_max - opening_min + 1), capped at the number of candidates."""
    cfg = _cfg(settings)
    ordered = priority_order(constraints, cfg)
    n = opening_size(persona_seed, len(ordered), cfg)
    return dedupe_areas(ordered, n)            # never two phrasings of one requirement; never more than the opening size


# ------------------------------------------------------------------ touched-constraint matcher
def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t


def tokens(text: Optional[str]) -> set[str]:
    return {_stem(t) for t in _WORD.findall((text or "").lower())}


def _name_tokens(name: Optional[str], stop: set[str]) -> set[str]:
    return {t for t in tokens(name) if t not in stop and not t.isdigit()}


def touched(c: BuyerConstraint, utterance: str) -> bool:
    """True when ``utterance`` (negotiator text) refers to constraint ``c`` (rules in the module docstring)."""
    u = tokens(utterance)
    if not u:
        return False
    stop = set(STOP) | tokens(c.unit)
    for name in (c.attribute, c.spec_name):
        n = _name_tokens(name, stop)
        if n and len(n & u) / len(n) >= 0.5:
            return True
    distinctive = {t for t in tokens(c.commercial_text)
                   if len(t) >= 5 and t.isalpha() and t not in stop}
    return bool(distinctive & u)
