"""Government-term blocklist (D027) and negotiator-input refusal (CLAUDE.md 1.5)."""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# Hidden-information markers the negotiator must never receive (keys or string values).
NEGOTIATOR_FORBIDDEN_TOKENS = (
    "golden_param", "tau", "τ", "bid_id", "gem_bid_id", "bid_ref",
    "product_target", "search_keyword", "relevance_score",
    "scenario_role", "evidence_coverage", "synthetic_commercial", "merged_from", "verification_only",  # D048/D049/D051
)
_GEM_BID_NUMBER = re.compile(r"GEM/\d{4}/B/\d+", re.I)
# Internal identifiers that must not appear in any utterance.
_UTTERANCE_LEAKS = (
    ("hidden id", re.compile(r"GEM/\d{4}/B/\d+|_C_[0-9a-f]{12}", re.I)),
    ("internal term", re.compile(r"(?<![A-Za-z])(?:tau|τ|golden_param|medium_strict|persona|hardness threshold)(?![A-Za-z])", re.I)),
)


def government_terms(settings: dict) -> list[str]:
    terms = settings.get("framing", {}).get("government_terms")
    if not terms:
        raise ValueError("settings framing.government_terms is missing or empty")
    return list(terms)


def _term_regex(term: str) -> re.Pattern:
    pat = r"\s+".join(re.escape(part) for part in term.split())
    return re.compile(r"(?<![A-Za-z0-9])" + pat + r"(?![A-Za-z0-9])", re.I)


def find_government_terms(text: Optional[str], terms: Iterable[str]) -> list[str]:
    """Blocklist terms present in ``text`` (case-insensitive, whole word/phrase), in list order."""
    if not text:
        return []
    return [t for t in terms if _term_regex(t).search(text)]


def remove_government_terms(text: str, terms: Iterable[str]) -> str:
    """``text`` with every blocklist term cut out and the spaces collapsed (D109: labels built from attribute names)."""
    for t in terms:
        text = _term_regex(t).sub(" ", text)
    return " ".join(text.split())


_SNAKE = re.compile(r"(?<![A-Za-z0-9_/.])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])")
_INTERNAL_PHRASES = re.compile(
    r"\b(?:budget[\s_-]+inr|qty[\s_-]+vs[\s_-]+moq|preferred[\s_-]+states|p[\s_-]+feasible|match[\s_-]+original|"
    r"spec[\s_-]+match|price[\s_-]+score|moq[\s_-]+fit|location[\s_-]+match|rating[\s_-]+bayes|"
    r"evidence[\s_-]+confidence|verification[\s_-]+only)\b", re.I)


def internal_name_problem(text: str) -> Optional[str]:
    """D072: internal attribute names (snake_case tokens, 'budget inr', 'qty vs moq', ...) are never spoken."""
    m = _SNAKE.search(text) or _INTERNAL_PHRASES.search(text)
    if m:
        return (f"the text speaks an internal name {m.group(0)!r}; say it in plain business words "
                "(for example 'budget', 'delivery time', 'supplier location')")
    return None


def utterance_problem(text: str, terms: Iterable[str]) -> Optional[str]:
    """Error message if an utterance has a government term or an internal-identifier leak."""
    hits = find_government_terms(text, terms)
    if hits:
        return f"the text contains forbidden government/tender term(s): {', '.join(repr(h) for h in hits)}; rewrite without them"
    for label, rx in _UTTERANCE_LEAKS:
        m = rx.search(text)
        if m:
            return f"the text leaks a {label}: {m.group(0)!r}; remove it"
    if not text.strip():
        return "the utterance is empty"
    return None


class NegotiatorInputError(ValueError):
    """Raised when a negotiator prompt input contains hidden buyer-side information."""


def _token_regex(tok: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(tok) + r"(?![A-Za-z0-9_])", re.I)


_TOKEN_RX = [(t, _token_regex(t)) for t in NEGOTIATOR_FORBIDDEN_TOKENS]


def _scan(obj: Any, path: str) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _check_string(str(k), f"{path}.<key {k}>")
            _scan(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple, set)):
        for i, v in enumerate(obj):
            _scan(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        _check_string(obj, path)


def _check_string(s: str, path: str) -> None:
    for tok, rx in _TOKEN_RX:
        if rx.search(s):
            raise NegotiatorInputError(f"negotiator input contains forbidden token {tok!r} at {path}")
    if _GEM_BID_NUMBER.search(s):
        raise NegotiatorInputError(f"negotiator input contains a bid number pattern at {path}")


def assert_negotiator_safe(inputs: Any) -> None:
    """Raise NegotiatorInputError if any key or string value (recursively) is hidden information."""
    _scan(inputs, "inputs")


# ------------------------------------------------------------------ final-turn text checks (D065 / D069)
_STAT_WORDS = (
    re.compile(r"\bprobabilit\w*|\bp_feasible\b|\bjoint\s+gain\b|\bfeasib\w*|\bcandidates?\b", re.I),
    re.compile(r"\bscor(?:e|es|ed|ing)\b|\bthreshold\b|\btiers?\b|\bcost\s*[:=]?\s*0?\.\d+|\bmatch(?:es)?\s*[:=]?\s*\d+\s*%", re.I),
    re.compile(r"\bHARD\b|\bSOFT\b"),
)
_BULLET_LINE = re.compile(r"^\s*(?:[-*\u2022\u25cf\u2013]|\d+\s*[.)]|\(?[a-z]\))\s+", re.I)
_INLINE_NUM = re.compile(r"(?:(?<=\s)|^)(\d{1,2})[.)]\s+(?=\S)")


def stat_word_problem(text: str) -> Optional[str]:
    """Error text for bracket tags, internal statistics words, or an enumerated burst (> 2 items) in an utterance."""
    if "[" in text or "]" in text:
        return "the text contains square brackets or a tag; write plain prose without [ ] tags"
    for rx in _STAT_WORDS:
        m = rx.search(text)
        if m:
            return (f"the text uses an internal/statistical term {m.group(0)!r} (scores, probabilities, counts of "
                    "candidates, costs, tiers, hardness labels); speak in plain business language instead")
    lines = sum(1 for ln in text.splitlines() if _BULLET_LINE.match(ln))
    nums = {int(m.group(1)) for m in _INLINE_NUM.finditer(text)}
    run = 0
    while run + 1 in nums:
        run += 1
    if max(lines, run) > 2:
        return "the text is a numbered or bulleted list of more than two items; write flowing sentences instead"
    return None


# ------------------------------------------------------------------ D070: supplier rating / years of experience are never spoken
# Deliberately limited to SUPPLIER phrasing: product specs such as "motor rating 1.5 HP", "BEE 3 star rating",
# "IP65 rating", "pressure rating PN 16", "rated for 10 bar" and "warranty of 2 years" must pass.
_SUPPLIER_WORD = r"(?:supplier|seller|vendor|manufacturer|dealer|firm|company|companies|suppliers|sellers|vendors)"
_STAT_NUM = r"[0-5](?:\.\d+)?(?![\d.]*\d)"
_SUPPLIER_STATS = (
    ("rating", re.compile(
        rf"\b{_SUPPLIER_WORD}(?:'s|s')?\s+(?:\w+\s+){{0,2}}(?:ratings?|rated|reviews?|stars?|star[- ]rating)\b", re.I)),
    ("rating", re.compile(rf"\b(?:ratings?|reviews?|stars?)\s+(?:of|from|by|given\s+by)\s+(?:other\s+|past\s+|previous\s+)?(?:buyers|customers|clients|users)\b", re.I)),
    ("rating", re.compile(rf"\brated\s+(?:at\s+least\s+|above\s+|over\s+|minimum\s+|a\s+minimum\s+of\s+)?{_STAT_NUM}\b(?!\s*(?:phase|pole|poles|hp|kw|v|volts?|a|amps?|bar|mm|kg|w|kva|lpm|rpm|hz|m|l)\b)", re.I)),
    ("rating", re.compile(r"\brated\s+(?:at\s+least\s+|above\s+|over\s+)?\w*\s*(?:stars?|out\s+of\s+(?:5|five))\b", re.I)),
    ("rating", re.compile(rf"\b{_STAT_NUM}\s*(?:/\s*5\b|out\s+of\s+(?:5|five)\b)", re.I)),
    ("rating", re.compile(r"\bout\s+of\s+(?:5|five)(?:\s+stars?)?\b", re.I)),
    ("rating", re.compile(r"\b(?:highly|top|well|best|poorly|lowly)[- ]rated\b", re.I)),
    ("rating", re.compile(rf"\bratings?\s+(?:of\s+|at\s+least\s+|above\s+|over\s+|minimum\s+|min\.?\s+)?{_STAT_NUM}\s*(?:\+|or\s+(?:above|higher|more|better))", re.I)),
    ("rating", re.compile(r"\b(?:customer|buyer|supplier|seller|vendor|marketplace|review)\s+ratings?\b", re.I)),
    ("rating", re.compile(r"\bratings?\s+(?:for|of|from)\s+(?:the\s+|your\s+|our\s+)?(?:suppliers?|sellers?|vendors?)\b", re.I)),
    ("experience", re.compile(r"\b(?:years?|yrs?)\s+(?:of\s+|in\s+)(?:experience|trading|business|operations?|the\s+(?:business|market|industry|trade))\b", re.I)),
    ("experience", re.compile(r"\b(?:trading|business|operating|track)\s+(?:history|record|experience)\b", re.I)),
    ("experience", re.compile(r"\b(?:in\s+business|tenure|trading\s+for)\b", re.I)),
    ("experience", re.compile(r"\b(?:well[- ])?established\s+(?:in|since|for|over|more\s+than|at\s+least|\d)", re.I)),
    ("experience", re.compile(r"\bwell[- ]established\b", re.I)),
    ("experience", re.compile(r"\b(?:\d+(?:\.\d+)?\+?|several|many|decades?\s+of)\s*(?:years?|yrs?)\s+(?:old|established|trading|experienced|experience)\b", re.I)),
)


def supplier_stats_problem(text: str) -> Optional[str]:
    """D070: error text if an utterance states or asks about a supplier's rating / stars / years of experience
    (rating-like or trading-history phrasing). Product specs ("motor rating 1.5 HP", "warranty of 2 years") pass."""
    for kind, rx in _SUPPLIER_STATS:
        m = rx.search(text or "")
        if m:
            what = "a supplier rating" if kind == "rating" else "a supplier's years of experience or trading history"
            return (f"the text mentions {what} ({m.group(0)!r}); supplier ratings and years of experience are never "
                    "stated, asked about or listed by either side; remove it (location preferences may be mentioned)")
    return None
