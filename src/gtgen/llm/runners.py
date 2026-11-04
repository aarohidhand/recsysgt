"""Typed prompt runners: build messages from typed inputs, call LLMClient, validate, convert to contracts.

Every runner returns an ``LLMResult``: ``value`` (contract objects), ``llm_call_ids`` and ``qa_flags``.
D066: a call that still fails after all its retries (schema validation / output checks ``llm.max_retries``, HTTP retries
in the client) raises ``ScenarioSkipped``; callers skip that scenario/bid. Nothing is ever fabricated. The prep runners
doc_classify / align / doc_claims resolve what they can with the deterministic rules in ``rules.py`` first
(``LLMResult.paths`` counts rule / llm / prefilter / cache items); only ambiguous items go to the LLM.
Auth failures (HTTP 401/402/403) still propagate (they are not a per-scenario problem); a budget error
(``LLMBudgetError``) is converted to ``ScenarioSkipped`` so one oversized bid never aborts a batch run.  Retries keep
only the most recent rejected answer plus the correction, so the prompt cannot grow past the client's budget.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Optional, Type, TypeVar

from pydantic import BaseModel

from gtgen.llm.strata import with_location
from gtgen.contracts import (
    AttributeAlignment, BelievedConstraint, BuyerConstraint, BuyerProfile, CandidateConstraint,
    DocClaim, FramingDecision, Hardness, NormalisedValue, Persona, RelaxationBundle, Tier, TranscriptView,
)

from .batching import PlannedBatch, plan_batches
from .blocklist import (
    NegotiatorInputError, assert_negotiator_safe, government_terms, stat_word_problem, supplier_stats_problem,
    internal_name_problem, utterance_problem,
)
from .claims import maybe_claims, verify_quote
from .client import estimate_messages_tokens, estimate_tokens, schema_text
from .context import ConversationContext, MissingConversationContext, numbers_in, numbers_problem
from .exceptions import LLMBudgetError, LLMCallFailed, LLMNetworkError, LLMValidationError, ScenarioSkipped
from . import rules
from .numfmt import format_inr, format_quantity, tidy_numbers, tidy_value
from .prompts import PROMPT_NAMES, TEMPERATURE_FALLBACK, load_prompt
from .schemas import (
    WEIGHT_KEYS, AlignOutput, BuyerItemDecision, ClaimItem, ClaimOutput, DocClaimOutput, DocClassifyOutput, PersonaOutputV2,
    ClarifyOutput, EvidenceSnippet, ExtractOutput, InterpretItem, MenuPointView, PersonaOutputV3, ProofRequestView, InterpretOutput, NegotiatorReplyOutput, NormaliseOutput, PersonaOutput,
    ReframeOutput, RequirementStatement, RevealOutput, UtteranceOutput, NegotiatorFinalOutput,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)


@dataclass
class LLMResult(Generic[T]):
    value: T
    llm_call_ids: list[str] = field(default_factory=list)
    qa_flags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    # D066: items per resolution path for the batched prep runners: {"rule": n, "llm": n, "prefilter": n, "cache": n}.
    # The per-item path is in ``extra["item_paths"]`` ({item key: path}).  Empty for single-call runners.
    paths: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.qa_flags

    def require(self) -> T:
        """D054: the value, or raise ``LLMCallFailed`` when the call failed (never a stub / template)."""
        if self.value is None or (self.qa_flags and not self.value):
            raise LLMCallFailed("; ".join(self.qa_flags) or "no value")
        return self.value


@dataclass(frozen=True)
class DocClaimRequest:
    product_id: str
    constraint_ref: str    # constraint_id (truth) or belief_id (negotiator)
    doc_name: str
    listing_text: str


TIGHTNESS_HINTS = {
    0: "This buyer is extremely selective: choose values at the demanding end of each range.",
    1: "This buyer is very selective: choose values near the demanding end of each range.",
    2: "This buyer is selective: choose values on the demanding side of each range.",
}
BEHAVIOUR_BY_TIER = {
    "lenient": "easygoing and accommodating; glad to meet a reasonable supplier halfway",
    "medium": "pragmatic; flexible on minor points and protective of what really matters",
    "medium_strict": "careful and somewhat guarded; concedes slowly and prefers to counter-offer rather than accept at once",
    "strict": "demanding and sceptical; rarely gives ground, pushes back firmly and wants claims proven",
}
_KNOWN_TERMS = ("budget_inr", "qty_vs_moq", "preferred_states")
# D070: supplier rating and years of experience are ranking-only features; never requested from the persona LLM, never
# validated, and silently dropped when old bands / old model output still carry them.
_RANKING_ONLY_TERMS = ("min_supplier_rating", "min_tenure_years")
NEGOTIATOR_PROMPTS = frozenset({"negotiator_extract", "negotiator_reply", "negotiator_clarify", "negotiator_interpret",
                                "negotiator_final", "doc_claim"})


def tightness_hint(target: Optional[int]) -> str:
    return TIGHTNESS_HINTS.get(target, "") if target is not None else ""


@dataclass(frozen=True)
class NormaliseRequest:
    text: str
    target: str            # e.g. "motor_rating"
    canonical_unit: str    # e.g. "HP"


def tier_for_tau(tau: float, settings: dict) -> Tier:
    """Deterministic tier binning from settings persona.tiers (upper bounds, exclusive)."""
    tiers = settings["persona"]["tiers"]
    for name, bound in sorted(tiers.items(), key=lambda kv: kv[1]):
        if tau < bound:
            return Tier(name)
    raise ValueError(f"tau {tau} above every tier bound in settings persona.tiers")


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _norm(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _same(a: Any, b: Any) -> bool:
    a, b = _norm(a), _norm(b)
    if a == b:
        return True
    try:
        return a is not None and b is not None and float(a) == float(b)
    except ValueError:
        return False


def _transcript_dicts(t: TranscriptView, last_n: Optional[int] = None) -> list[dict]:
    turns = t.turns[-last_n:] if last_n else t.turns
    return [{"turn_idx": x.turn_idx, "speaker": x.speaker.value, "utterance": x.utterance} for x in turns]


def build_negotiator_extract_variables(transcript: TranscriptView) -> dict:
    """Prompt variables for negotiator_extract. Accepts a TranscriptView only; refuses hidden info."""
    if not isinstance(transcript, TranscriptView):
        raise TypeError("negotiator_extract accepts only a TranscriptView")
    variables = {"transcript_json": _transcript_dicts(transcript)}
    assert_negotiator_safe(variables)
    return variables


def _last_utterance(t: TranscriptView, speaker: str) -> str:
    for turn in reversed(t.turns):
        if turn.speaker.value == speaker:
            return turn.utterance
    return ""


def build_negotiator_reply_variables(
    transcript: TranscriptView, bundle: Optional[RelaxationBundle], beliefs: Optional[list[BelievedConstraint]],
    evidence: list[EvidenceSnippet], *, sales_persona: Optional[dict] = None,
    menu_point: Optional[MenuPointView] = None, allowed_numbers: Optional[list] = None,
    reply_kind: str = "PROPOSE", proof_requests: Optional[list[ProofRequestView]] = None,
) -> dict:
    """Prompt variables for negotiator_reply v3. TranscriptView / belief / catalogue-evidence inputs only.

    PROPOSE needs ``menu_point`` or ``bundle`` + ``beliefs``; ACKNOWLEDGE needs ``proof_requests`` (D055).
    """
    if not isinstance(transcript, TranscriptView):
        raise TypeError("negotiator_reply accepts only a TranscriptView")
    if reply_kind not in ("PROPOSE", "ACKNOWLEDGE"):
        raise ValueError("reply_kind must be PROPOSE or ACKNOWLEDGE")
    menu: Optional[dict] = None
    nums: list[Any] = list(allowed_numbers or [])
    if reply_kind == "PROPOSE":
        if menu_point is not None:
            menu = menu_point.model_dump(exclude_none=True)
        elif bundle is not None:
            by_id = {b.belief_id: b for b in (beliefs or [])}
            items = []
            for it in bundle.items:
                if it.constraint_ref not in by_id:
                    raise ValueError(f"bundle item refers to unknown belief_id {it.constraint_ref!r}")
                items.append({"attribute": by_id[it.constraint_ref].attribute, "kind": it.kind,
                              "from_value": it.from_value, "to_value": it.to_value})
            menu = {"items": items, "extra_options": bundle.joint_gain}
        else:
            raise ValueError("PROPOSE needs a menu_point or a bundle")
        nums += [menu.get("extra_options")]
        for it in menu["items"]:
            nums += [it.get("from_value"), it.get("to_value")]
        for it in menu["items"]:              # D065: no float artefacts in what the model sees; both forms stay allowed
            for k in ("from_value", "to_value"):
                if it.get(k) is not None:
                    nums.append(tidy_value(it[k]))
                    it[k] = tidy_value(it[k])
    else:
        if not proof_requests:
            raise ValueError("ACKNOWLEDGE needs proof_requests")
        ev_ids = {e.evidence_id for e in evidence}
        missing = [p.evidence_id for p in proof_requests if p.evidence_id not in ev_ids]
        if missing:
            raise ValueError(f"proof request evidence ids {missing} are not in the supplied evidence")
        nums += [p.doc_name for p in proof_requests]
    variables = {
        "sales_persona_json": sales_persona,
        "reply_kind": reply_kind,
        "transcript_json": _transcript_dicts(transcript),
        "last_buyer_message": _last_utterance(transcript, "BUYER"),
        "menu_json": menu,
        "proof_requests_json": [p.model_dump() for p in (proof_requests or [])] if reply_kind == "ACKNOWLEDGE" else [],
        "allowed_numbers_json": [n for n in nums if n is not None],
        "evidence_json": [e.model_dump() for e in evidence],
    }
    assert_negotiator_safe(variables)
    return variables


def _norm_topic(t: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(t).lower()).split())


def map_asked_topic(echo: str, allowed: list[str]) -> Optional[str]:
    """D073: map a (possibly trimmed/reworded) echoed topic to exactly one canonical allowed topic, else None."""
    e = _norm_topic(echo)
    if not e:
        return None
    norm = [(a, _norm_topic(a)) for a in allowed]
    for a, n in norm:
        if n == e:
            return a
    et = set(e.split())

    def dice(n: str) -> float:
        nt = set(n.split())
        return 2 * len(et & nt) / (len(et) + len(nt)) if et and nt else 0.0
    for test in (lambda n: n.startswith(e) or e.startswith(n) or e in n or n in e,
                 lambda n: dice(n) >= 0.6):
        hits = [a for a, n in norm if n and test(n)]
        if len(hits) == 1:
            return hits[0]
    return None


def build_negotiator_clarify_variables(
    transcript: TranscriptView, topics: list[str], known: list[str], sales_persona: Optional[dict] = None,
    already_asked: Optional[list[str]] = None,
) -> dict:
    """Prompt variables for negotiator_clarify v4. TranscriptView + catalogue topic labels + sales persona only.
    ``already_asked`` = topic labels asked in earlier clarify turns (D070: never repeated)."""
    if not isinstance(transcript, TranscriptView):
        raise TypeError("negotiator_clarify accepts only a TranscriptView")
    variables = {
        "sales_persona_json": sales_persona,
        "transcript_json": _transcript_dicts(transcript),
        "last_buyer_message": _last_utterance(transcript, "BUYER"),
        "topics_json": [str(t) for t in topics],
        "known_json": [str(k) for k in known],
        "asked_json": [str(a) for a in (already_asked or [])],
    }

    assert_negotiator_safe(variables)
    return variables


def build_negotiator_interpret_variables(transcript: TranscriptView, proposal_items: list[dict]) -> dict:
    """Prompt variables for negotiator_interpret. ``proposal_items`` = the negotiator's own last proposal."""
    if not isinstance(transcript, TranscriptView):
        raise TypeError("negotiator_interpret accepts only a TranscriptView")
    items = [{"index": int(i["index"]), "attribute": str(i["attribute"]), "kind": str(i["kind"]),
              "from_value": None if i.get("from_value") is None else str(i["from_value"]),
              "to_value": None if i.get("to_value") is None else str(i["to_value"])}
             for i in proposal_items]
    variables = {"transcript_json": _transcript_dicts(transcript), "proposal_items_json": items}
    assert_negotiator_safe(variables)
    return variables


# ------------------------------------------------------------------ D069 final turn
FINAL_KINDS = ("recommend", "best_available")
_ORG_WORDS = frozenset({"pvt", "ltd", "limited", "industries", "enterprises", "traders", "works", "systems", "pumps",
                        "motors", "corp", "corporation", "company", "co", "llp", "engineering", "exports", "agencies",
                        "solutions", "manufacturers", "suppliers", "electricals", "machines", "tools"})
_CAP_SPAN = re.compile(r"\b[A-Z][\w&'.-]*(?:\s+[A-Z][\w&'.-]*)*\b")
_WORD_RX = re.compile(r"[A-Za-z0-9]+")


def _fold(s: str) -> str:
    return " ".join(_WORD_RX.findall(s.casefold()))


def sales_persona_line(sales_persona: Any) -> str:
    """Readable tone line for a sales persona (dict from scenario_meta, a SalesPersonaOut, or a ready string).

    D078: no name any more - the negotiator never introduces itself by name, so there is nothing to render but
    the style/approach (a legacy ``name`` key in an old dict is simply ignored, never rendered)."""
    if not sales_persona:
        return "none"
    if isinstance(sales_persona, BaseModel):
        sales_persona = sales_persona.model_dump()
    if isinstance(sales_persona, dict):
        parts = [str(sales_persona.get(k)).strip().rstrip(".") for k in ("speaking_style", "sales_approach")
                 if sales_persona.get(k)]
        return ". ".join(parts).strip() or "none"
    return str(sales_persona)


_LABEL_LISTS = ("met", "unmet", "to_confirm", "unverified")
_STAT_LABEL = re.compile(r"\b(?:supplier|seller|vendor)\b.*\b(?:rating|ratings|tenure|years|experience|reviews?)\b|"
                         r"\b(?:tenure|years\s+in\s+business|trading\s+history|years\s+of\s+experience)\b", re.I)


_LABEL_WORDS = {
    "budget_inr": "budget", "budget inr": "budget", "qty_vs_moq": "minimum order quantity against your quantity",
    "preferred_states": "supplier location", "preferred states": "supplier location",
    "min_supplier_rating": "supplier rating", "inr": "rupees",
}
_LABEL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+|\bbudget inr\b|\bpreferred states\b", re.I)


def humanise_label(label: str) -> str:
    """D072: internal attribute names in a facts label are mapped to plain words (budget_inr -> budget, qty_vs_moq ->
    minimum order quantity against your quantity, any other snake_case -> words with spaces)."""
    def one(m: re.Match) -> str:
        k = m.group(0).lower()
        return _LABEL_WORDS.get(k, k.replace("_", " "))
    return _LABEL_TOKEN.sub(one, label)


def scrub_supplier_stat_labels(facts: dict) -> dict:
    """D070: a copy of ``facts`` without met / unmet / to_confirm labels about supplier rating or years of experience
    (those are ranking-only; they are never listed as met or unmet)."""
    out = {k: v for k, v in facts.items() if k != "close"}      # D072: unknown keys such as 'close' are ignored
    if isinstance(out.get("to_confirm"), list):
        out["to_confirm"] = [humanise_label(x) if isinstance(x, str) else x for x in out["to_confirm"]]
    out["options"] = []
    for o in facts["options"]:
        o2 = dict(o)
        for k in _LABEL_LISTS:
            if isinstance(o2.get(k), list):
                o2[k] = [humanise_label(x) for x in o2[k]
                         if not (isinstance(x, str) and (_STAT_LABEL.search(x) or supplier_stats_problem(x)))]
        out["options"].append(o2)
    return out


def build_negotiator_final_variables(kind: str, facts: dict, sales_persona: Optional[Any]) -> dict:
    """Facts shape (D070): ``options`` in RANK ORDER, each ``{"rank", "product", "supplier", "price_text", "met": [...],
    "unmet": [...] (verified shortfalls), "to_confirm": [...] (UNKNOWN / not yet evidenced)}`` (legacy ``unverified`` is read
    as ``to_confirm``); optional top-level ``to_confirm`` and ``note``."""
    if kind not in FINAL_KINDS:
        raise ValueError(f"negotiator_final kind must be one of {FINAL_KINDS}")
    if not isinstance(facts, dict) or not isinstance(facts.get("options"), list) or not facts["options"]:
        raise ValueError("negotiator_final needs facts['options'] (a non-empty list)")
    for o in facts["options"]:
        if not isinstance(o, dict) or not o.get("product") or "rank" not in o:
            raise ValueError("every option needs 'rank' and 'product'")
    facts = scrub_supplier_stat_labels(facts)
    variables = {"sales_persona_json": sales_persona_line(sales_persona), "kind": kind, "facts_json": facts}
    assert_negotiator_safe(variables)
    return variables


_SHORTFALL = re.compile(
    r"(?<!\w)(?:fall(?:s|ing)?\s+short|short\s+of|misses|missing|lacks?|lacking|falls?\s+below|fail(?:s|ed)?\s+to|shortfall|unmet|"
    r"(?:does|do|did)\s*(?:not|n't)\s+(?:meet|match|satisfy|have|offer|include|come\s+with|provide|support|reach)|"
    r"(?:is|are)\s*(?:not|n't)\s+(?:met|available|certified|compliant))", re.I)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_CLAUSE_SPLIT = re.compile(r"[,;:]|\b(?:though|but|while|whereas|although|however|and yet)\b", re.I)


def _option_lists(o: dict) -> tuple[list[str], list[str], list[str]]:
    met = [str(x) for x in (o.get("met") or [])]
    unmet = [str(x) for x in (o.get("unmet") or [])]
    tc = [str(x) for x in (o.get("to_confirm") or [])] + [str(x) for x in (o.get("unverified") or [])]
    return met, unmet, tc


def _option_key(o: dict) -> str:
    return _fold(str(o.get("supplier") or o["product"]))


_TRAILING_OFFER = re.compile(
    r"\b(?:would\s+you\s+(?:like|prefer|want)|shall\s+(?:i|we)|should\s+(?:i|we)\s+(?:go|proceed|check|look|arrange)|"
    r"do\s+you\s+want\s+(?:me|us)\s+to|do\s+you\s+(?:wish|need)\s+(?:me|us)\s+to|let\s+(?:me|us)\s+know|"
    r"feel\s+free\s+to|please\s+(?:let|tell|revert|reply|respond)|get\s+back\s+to\s+(?:me|us)|"
    r"which\s+(?:of\s+these|one|option)\s+(?:would|do)\s+you|happy\s+to\s+(?:help|assist)\s+(?:further|more))\b", re.I)


def closing_problem(text: str) -> Optional[str]:
    """D072: the final turn CLOSES the conversation: no question mark and no invitation to reply."""
    if "?" in text:
        return ("the closing message must not contain a question mark: it closes the conversation, so state the outcome and "
                "the outstanding items as plain statements")
    m = _TRAILING_OFFER.search(text)
    if m:
        return (f"the closing message must not invite a reply ({m.group(0)!r}); close with a statement of the outcome and "
                "the items still to be confirmed")
    return None


_FUTURE_ACTION = re.compile(
    r"(?<![\w])(?:"
    r"(?:i|we)\s*(?:will|shall|would\s+(?:still\s+|also\s+|then\s+)?(?:check|confirm|ask|verify|follow|reach|revert|get|take|move|note|arrange|come|connect|contact|look|look))\b"
    r"|(?:i|we)['\u2019](?:ll|d\s+(?:still\s+)?(?:check|confirm|ask|verify|follow|reach))\b"
    r"|let\s+(?:me|us)\b|(?:am|are|is|'m|\u2019m)\s+going\s+to\b|going\s+to\s+(?:check|confirm|reach|follow|take|move|ask|verify)\b"
    r"|(?:i|we)\s*(?:am|are|['\u2019]m|['\u2019]re)\s+(?:now\s+)?(?:taking|moving|checking|noting|reaching|following|arranging|confirming|verifying|asking)\b"
    r"|(?:i|we)\s+(?:will|shall)?\s*move\s+forward\b|\bwill\s+(?:have|get)\s+(?:the|this|that|these|those|it|them)\b[^.]{0,40}\b(?:checked|confirmed|verified)"
    r")", re.I)


def future_action_problem(text: str) -> Optional[str]:
    """D073: the closing message states the outcome; it makes no first-person promise of further action
    (I will / I'll / we will / let me / going to / I would check / I am taking forward ...)."""
    m = _FUTURE_ACTION.search(text)
    if m:
        return (f"the closing message must not promise further action ({m.group(0)!r}); state the outcome, and state "
                "what is still open as plain facts, for example 'Still open with the suppliers: ...', using only the "
                "labels given in the facts")
    return None


def final_text_problem(text: str, facts: dict, terms: list[str]) -> Optional[str]:
    """Code-side guard for the negotiator's final turn: plain prose; numbers and names only from ``facts``; options in the
    given (rank) order; only ``unmet`` items are described as shortfalls, ``to_confirm`` / ``met`` never (conservative)."""
    p = (_speech_problem(text, terms) or no_greeting_problem(text) or stat_word_problem(text)
         or closing_problem(text) or future_action_problem(text))
    if p:
        return p
    options = facts.get("options") or []
    sources: list[Any] = [json.dumps(facts, ensure_ascii=False), len(options)]
    p = numbers_problem(text, sources)
    if p:
        return p
    facts_fold = _fold(json.dumps({k: facts[k] for k in facts if k != "options"}, ensure_ascii=False) + " " +
                       " ".join(str(o.get(k) or "") for o in options for k in ("product", "supplier", "price_text")
                                ) + " " + " ".join(str(x) for o in options for k in _LABEL_LISTS
                                                   for x in (o.get(k) or [])))
    name_tokens = {t for o in options for k in ("product", "supplier") for t in _fold(str(o.get(k) or "")).split()}
    for m in _CAP_SPAN.finditer(text):
        words = m.group(0).split()
        if len(words) < 2:
            continue
        span = _fold(m.group(0))
        if span in facts_fold:
            continue
        toks = span.split()
        if any(t in _ORG_WORDS for t in toks) or any(t in name_tokens for t in toks):
            # drop a capitalised sentence-start word glued to a name ("Thanks Acme Pumps")
            if len(toks) > 2 and " ".join(toks[1:]) in facts_fold:
                continue
            return (f"the text mentions {m.group(0)!r}, which is not a product or supplier name from the facts; "
                    "use only the names given in the facts, exactly as written")
    low = _fold(text)
    missing = [str(o.get("supplier") or o["product"]) for o in options if _option_key(o) not in low]
    if missing:
        return f"the reply must name every option's supplier (or product) exactly as in the facts; missing {missing}"
    n = len([x for x in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if re.search(r"\w", x)])
    if n < 2 or n > 8:
        return f"the reply has {n} sentences; write 2 to 6 sentences"
    # D070: options in the order given (rank order). Options sharing a supplier name are skipped (ambiguous).
    keys = [_option_key(o) for o in options]
    uniq = [k for k in keys if keys.count(k) == 1]
    pos = [(low.find(k), k) for k in uniq]
    if any(a[0] > b[0] for a, b in zip(pos, pos[1:])):
        names = ", ".join(str(o.get("supplier") or o["product"]) for o in options)
        return f"present the options in the order given (first pick first): {names}"
    # D070: shortfall wording only for 'unmet' items; 'to_confirm' and 'met' are never described as shortfalls.
    # The option is the one named in the sentence (a sentence without a name continues the previous option); only the
    # CLAUSE holding the shortfall wording is searched for labels.
    current: Optional[dict] = None
    for sentence in _SENT_SPLIT.split(text.strip()):
        sf = _fold(sentence)
        named = [o for o in options if _option_key(o) in sf]
        if len(named) == 1:
            current = named[0]
        elif len(named) > 1:
            current = None
            continue
        if current is None:
            continue
        for clause in _CLAUSE_SPLIT.split(sentence):
            if not _SHORTFALL.search(clause):
                continue
            o = current
            met, unmet, tc = _option_lists(o)
            cf = _fold(clause)
            wrong = [lbl for lbl in met + tc if len(_fold(lbl)) >= 4 and _fold(lbl) in cf]
            who = str(o.get("supplier") or o["product"])
            if wrong:
                kind = "to_confirm (not yet known: say you would check it with the supplier)" if wrong[0] in tc else "met"
                return (f"{who}: {wrong[0]!r} is listed as {kind}, but the text describes it as a shortfall; only items in "
                        "'unmet' may be described as missing or falling short")
            if not unmet:
                return (f"{who}: the text describes a shortfall but the facts list nothing unmet for this option; "
                        "describe unknowns as points to check with the supplier")
    return None


# ------------------------------------------------------------------ D078 amendment: deterministic fallback text
# User decision (2026-11-01): every scenario must end with a recorded conversation and a top-candidate
# recommendation - a turn whose LLM wording keeps failing its check after every retry no longer aborts the whole
# scenario (D066/D054's "never fabricate, skip instead" rule is deliberately set aside here for exactly this case).
# Each builder below uses ONLY the same already-decided/verified data the model itself was given - no new claims,
# no invented numbers - so the result is as safe as a template can be. Call sites tag the qa_flags with
# "template_fallback:<prompt>" so this is always visible and auditable in the output, never silent.
def _join_list(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def opening_fallback_text(product_category: str, quantity: str, quantity_unit: str, budget_inr: Optional[str],
                          reqs: list[dict]) -> str:
    """Deterministic buyer opening: greeting + need + requirements, in the buyer's own already-tidied words."""
    parts = [f"Hello, we are looking to buy {quantity} {quantity_unit} of {product_category}."]
    if budget_inr:
        parts.append(f"Our budget is {budget_inr}.")
    for r in reqs:
        parts.append(str(r["text"]).rstrip(".") + ".")
    return " ".join(parts)


def buyer_reply_fallback_text(items: list[Any]) -> str:
    """Deterministic buyer reply: one plain sentence per already-decided item (YES/NO/COUNTER/BYPASS/PROOF_REQUEST)."""
    parts = []
    for i in items:
        item, action = str(getattr(i, "item", "") or ""), getattr(i, "action", "")
        if action == "YES":
            parts.append(f"{item} works for us.")
        elif action == "NO":
            parts.append(f"{item} does not work for us.")
        elif action == "COUNTER":
            cv = getattr(i, "counter_value", None)
            parts.append(f"On {item}, we would want {cv} instead." if cv else f"On {item}, we need something different.")
        elif action == "BYPASS":
            parts.append(f"{item} is not something we need to settle right now.")
        elif action == "PROOF_REQUEST":
            parts.append(f"Please send us the {getattr(i, 'doc_name', None) or 'documentation'} for {item}.")
        else:
            parts.append(f"Noted on {item}.")
    return " ".join(parts) or "Noted, thank you."


def missing_counter_values(items: list[Any], text: str) -> list[str]:
    """Counter values a buyer reply failed to state.

    A multi-item proposal can produce several COUNTER decisions in one turn.  The buyer must put every one of
    them into the utterance, otherwise the negotiator only sees (and can only answer) the counters that were
    spoken.  Numeric counters are compared by their numbers so "11.5" and "11.50" are the same; a categorical
    counter must appear as a word/phrase.
    """
    low = text.lower()
    missing: list[str] = []
    for i in items:
        if getattr(i, "action", "") != "COUNTER":
            continue
        cv = str(getattr(i, "counter_value", "") or "").strip()
        if not cv:
            continue
        wanted = numbers_in(cv)
        if wanted:
            if not wanted <= numbers_in(text):
                missing.append(cv)
        elif not re.search(r"(?<!\w)" + re.escape(cv.lower()) + r"(?!\w)", low):
            missing.append(cv)
    return missing


def buyer_reveal_fallback_text(req_texts: list[str]) -> str:
    """Deterministic buyer reveal: just states the (already-tidied) requirement text(s) being answered."""
    texts = [t.rstrip(".") + "." for t in dict.fromkeys(req_texts) if t]
    return "Yes, one more thing: " + " ".join(texts) if texts else "Yes, that is correct."


_KIND_PHRASE = {
    "numeric_widen": "Could we move {attribute} from {from_value} to {to_value}?",
    "drop": "Could we drop the {attribute} requirement?",
    "doc_to_pending": "Could we treat {attribute} as pending, to confirm with the supplier later?",
    "categorical_add": "Could we add {to_value} to the {attribute} options?",
}


def negotiator_propose_fallback_text(menu: dict) -> str:
    """Deterministic PROPOSE turn: one plain question per menu item, from the same menu the model was given."""
    parts = []
    for it in menu.get("items") or []:
        attribute = humanise_label(str(it.get("attribute") or ""))
        tpl = _KIND_PHRASE.get(it.get("kind"), "Could we adjust {attribute} from {from_value} to {to_value}?")
        parts.append(tpl.format(attribute=attribute, from_value=it.get("from_value"), to_value=it.get("to_value")))
    extra = menu.get("extra_options")
    if extra:
        parts.append(f"That would open up {extra} more option(s).")
    return " ".join(parts) or "Would you be open to a small change in your requirements?"


def negotiator_acknowledge_fallback_text(proof_requests: list[Any]) -> str:
    """Deterministic ACKNOWLEDGE turn: confirms each requested document by name, from the proof requests given."""
    parts = [f"I can confirm the {pr.doc_name} for {humanise_label(pr.attribute)}." for pr in proof_requests]
    return " ".join(parts) or "I can confirm that."


def final_fallback_text(kind: str, facts: dict) -> str:
    """Deterministic closing turn: plain top-5 recommendation built only from the already-verified ``facts``
    (same shape negotiator_final's own check validates against), so the conversation always ends with one."""
    options = facts.get("options") or []
    lead = ("Here is what I can offer" if kind == "recommend" else
            "Nothing on the market meets every requirement, but here is the closest match")
    parts = [lead + ":"]
    for o in options[:5]:
        who = str(o.get("supplier") or o["product"])
        price = f" at {o['price_text']}" if o.get("price_text") else ""
        met, unmet, tc = _option_lists(o)
        bits = []
        if met:
            bits.append("meets " + _join_list(met))
        if unmet:
            bits.append("falls short on " + _join_list(unmet))
        if tc:
            bits.append("still to confirm with the supplier: " + _join_list(tc))
        detail = "; ".join(bits) if bits else "details to confirm with the supplier"
        parts.append(f"{o['product']} from {who}{price} - {detail}.")
    top_confirm = [str(x) for x in (facts.get("to_confirm") or [])]
    if top_confirm:
        parts.append("Still open with the supplier: " + _join_list(top_confirm) + ".")
    return " ".join(parts)


# ------------------------------------------------------------------ D065/D070 opening shape
_GREETING = re.compile(
    r"^\W*(?:hello|hi|hey|hullo|dear|greetings|good\s+(?:morning|afternoon|evening|day)|namaste|namaskar|namaskaram|vanakkam|"
    r"sat\s+sri\s+akal|jai\s+\w+|salaam|salam|pranam|welcome)\b", re.I)
_ABBREV = re.compile(r"\b(Pvt|Ltd|Co|Inc|No|Nos|Rs|Mr|Mrs|Ms|Dr|St|approx|vs|etc|e\.g|i\.e)\.", re.I)


def sentence_count(text: str) -> int:
    """Sentences in a short spoken text (company abbreviations such as 'Pvt. Ltd.' and decimals do not split)."""
    t = _ABBREV.sub(lambda m: m.group(1), text.strip())
    return len([x for x in re.split(r"(?<=[.!?])\s+|\n+", t) if re.search(r"\w", x)])


def opening_problem(text: str, min_sentences: int = 2, max_sentences: int = 6) -> Optional[str]:
    """D065/D072 opening shape: starts with a greeting, ``min_sentences`` to ``max_sentences`` sentences (settings
    persona.realism.opening_min_sentences / opening_max_sentences)."""
    if not _GREETING.match(text):
        return ("the opening must START with a short greeting in your own voice (for example Hello, Hi, Good morning, "
                "Namaste), then the need")
    n = sentence_count(text)
    if not min_sentences <= n <= max_sentences:
        return f"the opening has {n} sentences; write {min_sentences} to {max_sentences} sentences"
    return None


_STAT_ATTR = re.compile(
    r"(?:supplier|seller|vendor|company|firm)_?(?:star_?)?(?:rating|ratings|reviews?|tenure|experience|years|age)|"
    r"^(?:customer|buyer|marketplace|review)_?(?:rating|ratings|score)$|"
    r"years_?(?:in_?business|of_?experience|of_?trading|trading|established)|trading_?(?:years|history|experience)|"
    r"^(?:min_)?(?:tenure|experience)(?:_years)?$|member_?since|^years_?established$", re.I)


def is_supplier_stat_attribute(attribute: str, value: Optional[str] = None, unit: Optional[str] = None) -> bool:
    """D070: a believed constraint about the supplier's rating or years of experience (never extracted, never believed)."""
    a = (attribute or "").strip()
    if _STAT_ATTR.search(a):
        return True
    return (unit or "").strip().lower() in ("stars", "star") and "motor" not in a.lower()


def no_greeting_problem(text: str) -> Optional[str]:
    """D078: a greeting word belongs only in the buyer's opening message; every later turn, either side, must not
    open with one (Namaste/Vanakkam/Hello/... repeated turn after turn reads as a verbal tic, not a conversation)."""
    if _GREETING.match(text):
        return ("do not open with a greeting word (Hello, Hi, Namaste, Vanakkam, Good morning, ...); a greeting "
                "belongs only in the very first message of the conversation, never a reply")
    return None


def _speech_problem(text: str, terms: list[str]) -> Optional[str]:
    """Blocklist + internal-id leak + D070 supplier rating / years of experience, for every spoken text.

    D078: deliberately does NOT include ``no_greeting_problem`` here - this is also reached, via
    ``LLMRunners._utterance_check``, by the buyer's OPENING turn, which must START with a greeting
    (``opening_problem`` requires it). Call sites for every turn that must NOT open with one (buyer_reply,
    negotiator_reply/acknowledge/final, buyer_reveal) add ``no_greeting_problem`` themselves."""
    return utterance_problem(text, terms) or supplier_stats_problem(text) or internal_name_problem(text)


_CLARIFY_OFF_TOPIC = (
    (re.compile(r"\b(?:brands?|make|manufacturers?)\b", re.I), "brand, make or manufacturer"),
    (re.compile(r"\bpackag\w*|\bpacking\b", re.I), "packaging"),
    (re.compile(r"\bwash\w*|\bcare\s+(?:label|tag|instruction)\w*", re.I), "wash care"),
    (re.compile(r"\bgsm\b", re.I), "GSM"),
    (re.compile(r"\bmoq\b|minimum\s+order", re.I), "minimum order quantity"),
)


def _first_word(text: str) -> str:
    m = re.match(r"\W*([A-Za-z]+)", text or "")
    return m.group(1).lower() if m else ""


# D072: words that signal an invented requirement in a clarify answer. Each stem may appear only if the requirement
# list (or the requirements already stated) contains the same stem.
_REVEAL_INVENTION_STEMS = (
    "certif", "authori", "turnover", "experience", "leather", "zipper", "polybag", "packag", "packing", "wash",
    "gsm", "brand", "oem", "warrant", "iso", "bis", "isi", "test report", "inspection", "sample", "insurance",
    "payment term", "credit", "advance", "audit", "licen", "label", "tag", "stitch", "fabric", "material", "grade",
)
_WHOLE_WORD_STEMS = frozenset({"oem", "iso", "bis", "isi", "gsm", "tag"})
_STEM_RX = {s: re.compile(r"(?<![a-z])" + re.escape(s) + (r"(?![a-z])" if s in _WHOLE_WORD_STEMS else ""), re.I)
            for s in _REVEAL_INVENTION_STEMS}


def reveal_scope_problem(text: str, requirement_texts: list[str], extra_allowed: Optional[list[str]] = None) -> Optional[str]:
    """D072: a clarify answer may only speak about the provided requirements. Numbers are checked by the caller; here a
    conservative stem list (certificate, authorization, turnover, experience, leather, zipper, packaging, wash, GSM,
    brand, OEM, warranty, ISO/BIS/ISI, sample, ...) must not appear unless the requirement texts contain that stem."""
    corpus = " ".join(requirement_texts + list(extra_allowed or []))
    for stem, rx in _STEM_RX.items():
        m = rx.search(text)
        if m and not rx.search(corpus):
            return (f"the answer mentions {m.group(0)!r}, which is not in the requirement list; answer only about the "
                    "requirements you were given (say whether each is firm or flexible, and its value if it was unclear) "
                    "and introduce nothing new")
    return None


_DEFERRAL = re.compile(
    r"\blater\b(?!\s+than)|\bfor\s+now\b|\bfor\s+the\s+time\s+being\b|\bat\s+this\s+(?:stage|point|moment)\b|"
    r"\bdown\s+the\s+(?:line|road)\b|\bin\s+due\s+course\b|\bat\s+a\s+later\b|\banother\s+time\b|\bnext\s+time\b|"
    r"\b(?:will|shall|can|could|may)\s+(?:discuss|revisit|cover|share|come\s+back|get\s+back|revert|talk|go\s+through)\b|"
    r"\b(?:we|i)['\u2019]ll\s+(?:discuss|revisit|cover|share|come\s+back|get\s+back|revert|talk|go\s+through|see)\b|"
    r"\blet['\u2019]?s\s+(?:talk|discuss|revisit|park)\b|\bwe\s+can\s+discuss\b|\bafter\s+you\s+show\b|\bonce\s+(?:we|i)\s+see\b", re.I)
_Q_STANCE = re.compile(r"\b(?:firm|flexib\w*|negotiab\w*|fixed|strict\w*|hard|soft|must|non-?negotiable|compromis\w*|stretch\w*|"
                       r"relax\w*|mandatory|essential|optional|important|bend|budge)\b", re.I)
_A_STANCE = re.compile(r"\b(?:firm|flexib\w*|negotiab\w*|fixed|strict\w*|must|need|needs|needed|require\w*|non-?negotiable|"
                       r"mandatory|essential|compulsory|ideally|prefer\w*|would\s+like|nice|wish|stretch\w*|room|adjust\w*|"
                       r"bend|budge|relax\w*|give|manage|accommodate|compromis\w*|can\s+live|not\s+a\s+(?:deal|hard)|"
                       r"hard\s+(?:limit|line|requirement)|cannot|can['\u2019]?t|will\s+not|won['\u2019]?t|up\s+to|at\s+least|"
                       r"at\s+most|maximum|minimum|no\s+more\s+than)\b", re.I)
_Q_VALUE = re.compile(r"\b(?:exact\w*|precise\w*|specific\w*|value|figure|number|how\s+much|how\s+many|what(?:'s|\s+is)\s+the)\b", re.I)
_REQ_STOP = frozenset({"with", "that", "this", "from", "have", "within", "should", "least", "most", "than", "more", "less",
                       "required", "preferably", "ideally", "about", "approx", "approximately", "minimum", "maximum", "upto"})


def _req_key_stems(req_text: str) -> list[str]:
    toks = [t for t in re.split(r"[^a-z]+", req_text.lower()) if len(t) >= 4 and t not in _REQ_STOP]
    return [t[:5] for t in toks]


def reveal_answer_problem(text: str, requirement_texts: list[str], last_question: str = "") -> Optional[str]:
    """D073: a clarify answer ANSWERS the clarified topic and does not defer.  Rejects deferral phrases ('we will discuss
    later', 'for now'), requires every clarified requirement to be mentioned (by one of its content words or its number),
    a stated stance (firm / flexible ...) when the question asks about hardness, and the value when it asks for the exact value."""
    m = _DEFERRAL.search(text)
    if m:
        return (f"the answer defers ({m.group(0)!r}); answer the negotiator's question now: say whether the requirement is "
                "firm or flexible, or give its exact value, and do not postpone anything")
    uniq = [r for r in requirement_texts if r and r.strip()]       # one text per clarified requirement
    tf = _fold(text)
    nums = numbers_in(text)
    asked_stance = bool(last_question and _Q_STANCE.search(last_question))
    asked_value = bool(last_question and _Q_VALUE.search(last_question))
    missing: list[str] = []
    for r in uniq:
        stems = _req_key_stems(r)
        rn = numbers_in(r)
        if (stems and any(st in tf for st in stems)) or (rn and rn & nums):
            continue
        if not stems and not rn:
            continue
        missing.append(r)
    if missing and len(missing) > (0 if len(uniq) <= 3 else len(uniq) // 3):
        return (f"the answer does not mention the requirement it was asked about ({missing[0]!r}); name that requirement "
                "and say what you were asked about it")
    if uniq and asked_stance and not _A_STANCE.search(text):
        return "the negotiator asked whether this is firm or flexible; say which, in your own words"
    if uniq and asked_value:
        want = set()
        for r in uniq:
            want |= numbers_in(r)
        if want and not (want & nums):
            return "the negotiator asked for the exact value; state the value from the requirement"
    return None


def reframe_value_problem(c: BuyerConstraint, text: Optional[str]) -> Optional[str]:
    """Error text when a KEEP/TRANSLATE commercial_text omits the constraint's value(s), else None.

    A constraint with allowed_values (or a numeric buyer_value) must have them stated: 'Cable size is in Sqmm.'
    (unit only) is rejected. More than 5 numeric allowed values may be summarised as a range, but with numbers."""
    def norm(s: str) -> str:
        # Whitespace around punctuation is not part of the value: "2,3,4" and "2, 3, 4" are the same.
        return re.sub(r"\s*([,;:/])\s*", r"\1", s)

    t = norm(text or "")
    av = [str(a).strip() for a in (c.allowed_values or []) if str(a).strip() and str(a) != "None"]
    bv = (c.buyer_value or "").strip()

    def has(tok: str) -> bool:
        return re.search(r"(?<![\w.])" + re.escape(norm(tok)) + r"(?![\w]|\.\d)", t, re.I) is not None

    def is_num(x: str) -> bool:
        return re.fullmatch(r"-?\d+(\.\d+)?", x) is not None

    if av:
        # GEM/2019/B/209245: buyer_value and allowed_values can disagree in the extracted data
        # ("THREE CORE ..." vs "TWO CORE ...").  The model correctly echoes buyer_value/raw_text;
        # either extracted form is a real value, so accept it rather than skip the bid.
        if bv and has(bv):
            return None
        if len(av) > 5 and all(is_num(a) for a in av):
            if re.search(r"\d", t):
                return None
        elif any(has(a) for a in av):
            return None
        # GEM/2019/B/242710: an allowed value can itself be an inequality range
        # (">= 1.10 & < 1.15").  The model writes "between 1.10 and 1.15"; accept that
        # when every number in the range appears in the text.
        for a in av:
            if re.search(r"[<>]=?|\bbetween\b|\bto\b", a, re.I):
                wanted = numbers_in(a)
                if wanted and wanted <= numbers_in(t):
                    return None
        # Fallback for long descriptive allowed-value lists (e.g. "Bronze grade LTB 2 of IS 318").
        # If the model states the category/grade (first two words) or any number from the value,
        # accept rather than aborting the scenario.
        for a in av:
            if len(a) > 10:
                words = [w for w in re.split(r"[^\w]+", a) if len(w) > 2 and w.lower() not in ("of","is","to","or","and")]
                if words and any(w.lower() in t.lower() for w in words[:3]):
                    return None
                nums = numbers_in(a)
                if nums and nums <= numbers_in(t):
                    return None
        return ("it must state the required value(s), not only the attribute or unit; include at least one of "
                f"{av[:6]} (or a numeric range if there are more than 5)")
    if bv and is_num(bv) and not has(bv):
        return f"it must state the required value {bv} {c.unit or ''}".rstrip()
    return None


class LLMRunners:
    def __init__(self, client: Any, settings: dict) -> None:
        self.client = client
        self.settings = settings
        self._llm = settings["llm"]
        self.terms = government_terms(settings)
        self.batch_max = int(self._llm["batch_max_constraints"])     # upper bound (quality guard); token budget / completion target may cut it lower (D062, D064)
        self._budget = int(self._llm["max_tokens_per_call"])
        self._max_comp = int(self._llm["max_completion_tokens"])
        # D064: planning bound for the OUTPUT of one batched call (<= the hard output cap), keeps batches reliable
        self._comp_target = min(int(self._llm.get("batch_completion_target", self._max_comp)), self._max_comp)
        self._min_comp = int(self._llm["min_completion_tokens"])
        self._max_listing = int(settings.get("doc_claims", {}).get("max_listing_chars", 10 ** 9))
        self.max_retries = int(self._llm["max_retries"])
        # D087: the closing turn falls back deterministically, so a long live retry chain only delays the bid.
        self.final_max_retries = int(self._llm.get("max_retries_final", self.max_retries))
        keys = tuple(settings["ranking"]["default_weights"])
        if set(keys) != set(WEIGHT_KEYS):
            raise ValueError(f"ranking.default_weights keys {sorted(keys)} != schema {sorted(WEIGHT_KEYS)}")
        # The schema order (not the settings order) is what the persona prompt shows and what keys the cache/fixtures.
        self.weight_keys = tuple(WEIGHT_KEYS)
        # D070: ranking weights of the supplier features are forced into this priority order (largest first).
        prio = (settings.get("ranking", {}).get("supplier_priority")
                or settings.get("recommendation", {}).get("supplier_priority")
                or ["location_match", "tenure_years", "rating_bayes"])
        self.supplier_priority = tuple(prio)
        if not set(self.supplier_priority) <= set(WEIGHT_KEYS):
            raise ValueError(f"supplier_priority {list(self.supplier_priority)} has keys outside the weight keys")

    # ------------------------------------------------------------ core
    def temperature_for(self, prompt_name: str) -> float:
        temps = self._llm.get("temperature", {})
        if prompt_name not in temps and prompt_name in TEMPERATURE_FALLBACK:
            prompt_name = TEMPERATURE_FALLBACK[prompt_name]       # D069: negotiator_final until settings has its own key
        if prompt_name not in PROMPT_NAMES or prompt_name not in temps:
            raise KeyError(f"llm.temperature has no key for prompt {prompt_name!r} (D028)")
        return float(temps[prompt_name])

    def _fits_budget(self, messages: list[dict[str, str]], model: Type[BaseModel]) -> bool:
        """Would the client send this request, i.e. does it leave at least ``min_completion_tokens`` of room?

        Mirrors ``LLMClient._build_budget`` exactly when available (it accounts for the prompt_json schema
        instruction and json_schema mode); test doubles fall back to the same estimate the planner uses."""
        build = getattr(self.client, "_build_budget", None)
        if callable(build):
            try:
                build(messages, model)
                return True
            except LLMBudgetError:
                return False
        estimate = getattr(self.client, "_input_estimate", None)
        inp = estimate(messages, model) if callable(estimate) else estimate_messages_tokens(
            messages, schema_text(model))
        return inp + self._min_comp <= self._budget

    def _call(
        self, name: str, variables: dict[str, Any], model: Type[M],
        check: Optional[Callable[[M], Optional[str]]] = None, seed: Optional[int] = None,
        version: int = 1, ctx: Optional[ConversationContext] = None, batch_info: Optional[dict] = None,
        max_retries: Optional[int] = None,
    ) -> tuple[Optional[M], list[str], Optional[str]]:
        """Run one prompt with schema + output-check retries. Returns (parsed, call_ids, None).

        D066: when every retry is used up (schema validation, output check, or HTTP retries) the call raises
        ``ScenarioSkipped`` -- it never returns a value or None; the caller skips the scenario/bid.  ``max_retries``
        overrides the global budget for prompts with a safe deterministic fallback (D087)."""
        retries = self.max_retries if max_retries is None else int(max_retries)
        if ctx is None and getattr(self.client, "strict", False):
            raise MissingConversationContext(
                f"{name}: live mode needs a ConversationContext (scenario_id, persona_seed, turn_idx) so that the "
                "cache never shares a response across conversations or turns (D054)")
        kw: dict[str, Any] = {"context": ctx.as_dict()} if ctx is not None else {}
        if batch_info is not None and self._accepts("batch_info"):
            kw["batch_info"] = batch_info   # D062: chosen batch size + estimated tokens, logged per request
        # D070/audit 10: negotiator-side prompts are scanned for hidden-field tokens right here (raises
        # NegotiatorInputError, never sent); the result is written to the provenance log per call.
        if name in NEGOTIATOR_PROMPTS:
            assert_negotiator_safe(variables)
        if self._accepts("barrier_scan"):
            kw["barrier_scan"] = "ok" if name in NEGOTIATOR_PROMPTS else "not_applicable"
        spec = load_prompt(name, version)
        if spec.output_schema != model.__name__:
            raise ValueError(f"prompt {name} declares {spec.output_schema}, runner uses {model.__name__}")
        temperature = self.temperature_for(spec.temperature_key)
        base = [{"role": "user", "content": spec.render(**variables)}]
        messages = list(base)
        ids: list[str] = []
        err: Optional[str] = None
        for attempt in range(retries + 1):
            # A retry must sample again: the same prompt would otherwise hit the cache entry written by the
            # rejected attempt and replay the same answer forever.  The attempt number is cache-key-only (never
            # rendered); attempt 0 keeps the original key so existing fixtures / warm caches stay valid.
            extra = variables if attempt == 0 else {**variables, "_retry_attempt": attempt}
            try:
                parsed, cid = self.client.call(
                    name, spec.version, messages, model, temperature=temperature, seed=seed,
                    extra_inputs=extra, **kw,
                )
            except LLMValidationError as exc:
                raise ScenarioSkipped(name, f"schema validation failed: {str(exc)[:300]}", ctx) from exc
            except LLMNetworkError as exc:
                if getattr(exc, "status", None) in (401, 402, 403):
                    raise                      # auth / payment: not a per-scenario problem
                raise ScenarioSkipped(name, f"HTTP failed after retries: {str(exc)[:300]}", ctx) from exc
            except LLMBudgetError as exc:
                # D066: a batch that cannot leave room for a completion is a per-scenario failure, never a reason
                # to abort the whole run (the planner already splits on tokens; this fires only for a huge item).
                raise ScenarioSkipped(name, f"prompt budget exceeded: {str(exc)[:300]}", ctx) from exc
            ids.append(cid)
            err = check(parsed) if check else None
            if err is None:
                return parsed, ids, None
            correction = {"role": "user",
                          "content": f"Your answer was rejected: {err}. Return the corrected JSON."}
            rejected = {"role": "assistant", "content": parsed.model_dump_json()}
            # Keep the retry prompt bounded: the original prompt + the most recent rejected answer + the
            # correction (not the whole history).  If even that leaves too little completion room, drop the
            # rejected answer - the error text alone still says what to fix - so retries cannot grow the prompt
            # past the client's budget.
            retry = base + [rejected, correction]
            messages = retry if self._fits_budget(retry, model) else base + [correction]
        raise ScenarioSkipped(name, f"output check failed after {retries} retries: {err}", ctx)

    def _call_or_fallback(
        self, name: str, variables: dict[str, Any], model: Type[M],
        check: Optional[Callable[[M], Optional[str]]], seed: Optional[int], version: int,
        ctx: Optional[ConversationContext], make_fallback: Callable[[], str],
        max_retries: Optional[int] = None,
    ) -> tuple[Optional[M], str, list[str], list[str]]:
        """D078 amendment (user decision, 2026-11-01): ``self._call`` wrapped so a turn NEVER aborts the whole
        scenario - when every retry is exhausted (``ScenarioSkipped``, whatever the reason), a deterministic
        fallback utterance from ``make_fallback()`` is used instead, tagged with a ``template_fallback:<name>``
        qa_flag so the substitution is always visible in the output. Returns (parsed_or_None, utterance,
        llm_call_ids, qa_flags); callers that need another field off ``parsed`` (e.g. cited_evidence_ids) check
        it for None first and fall back to an empty/default value."""
        try:
            parsed, ids, err = self._call(name, variables, model, check, seed, version=version, ctx=ctx,
                                          max_retries=max_retries)
        except ScenarioSkipped as exc:
            return None, make_fallback(), [], [f"template_fallback:{name}:{exc.last_error[:160]}"]
        if parsed is None:          # a ``_call`` override (e.g. the call-plan dry-run) swallowed the skip itself
            return None, make_fallback(), ids, [f"template_fallback:{name}:{str(err)[:160]}"]
        return parsed, parsed.utterance.strip(), ids, []

    def _accepts(self, param: str) -> bool:
        """Test doubles with a fixed ``call`` signature (no such parameter / ``**kw``) simply do not get it."""
        import inspect
        try:
            ps = inspect.signature(self.client.call).parameters
        except (TypeError, ValueError):
            return False
        return param in ps or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in ps.values())

    def _accepts_batch_info(self) -> bool:
        return self._accepts("batch_info")

    def _flag(self, name: str, err: str, where: str = "") -> str:
        return f"llm_failed:{name}{(':' + where) if where else ''}:{err[:200]}"

    # ------------------------------------------------------------ D066 path accounting
    def _llm_path(self, call_id: str) -> str:
        """'cache' when the client served this call id from cache / fixture, else 'llm'."""
        src = getattr(self.client, "source_of", None)
        try:
            return "cache" if callable(src) and src(call_id) in ("cache_hit", "fixture") else "llm"
        except Exception:                      # a test double must never break path accounting
            return "llm"

    @staticmethod
    def _note_path(res: "LLMResult", key: str, path: str) -> None:
        res.extra.setdefault("item_paths", {})[key] = path
        res.paths[path] = res.paths.get(path, 0) + 1

    def _log_paths(self, prompt: str, res: "LLMResult", ctx: Optional[ConversationContext] = None) -> None:
        """D066/D069: log the per-prompt counts and PERSIST path=rule|llm|prefilter|cache per item (client provenance log)."""
        p = res.paths
        logger.info("%s: n_rule=%d n_llm=%d n_prefilter=%d n_cache=%d", prompt, p.get("rule", 0), p.get("llm", 0),
                    p.get("prefilter", 0), p.get("cache", 0))
        rec = getattr(self.client, "record_prep_paths", None)
        if callable(rec):
            try:
                rec(prompt, dict(p), dict(res.extra.get("item_paths", {})), ctx.as_dict() if ctx is not None else None)
            except Exception:                  # provenance must never break a run
                logger.warning("could not persist prep paths for %s", prompt)

    # ------------------------------------------------------------ token-aware batch planning (D062; no network)
    # Expected reply size per item (tokens).  Deliberately generous; the real usage is logged next to it.
    # reframe echoes the item (golden_param, operator, value, unit) and adds commercial_text + reason.
    _COMP_DOC_CLASSIFY = 45     # idx + is_doc_proof + doc_name
    _COMP_ALIGN = 130           # buyer_attribute + up to a few {candidate_attribute, confidence}
    _COMP_NORMALISE = 60        # item_id + value + unit + confidence
    # item_id + claims + verbatim quote: the JSON skeleton plus a quote at the rule/prompt cap (200 chars), estimated with
    # the same estimator as the prompt (D071: derived, not a fixed number; a 24-item batch stays under the completion target)
    _COMP_DOC_CLAIM = estimate_tokens('{"item_id":99,"claims":true,"quote":""}') + estimate_tokens("x" * rules.CLAIM_MAX_QUOTE_CHARS)

    @staticmethod
    def _item_tokens(obj: Any) -> int:
        return estimate_tokens(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)) + 2

    def _pack(self, name: str, model: Type[BaseModel], variables_empty: dict, views: list, completion: Callable[[Any], int],
              max_items: Optional[int] = None, version: int = 1) -> list[PlannedBatch]:
        spec = load_prompt(name, version)
        fixed = estimate_messages_tokens([{"role": "user", "content": spec.render(**variables_empty)}],
                                         schema_text(model)) + 8
        return plan_batches(
            views, fixed, self._item_tokens, completion, self._budget,
            max_items=self.batch_max if max_items is None else min(self.batch_max, max_items),
            fixed_completion_tokens=24, max_completion_tokens=self._comp_target, min_completion_tokens=self._min_comp)

    def plan_reframe(self, product_category: str, constraints: list[BuyerConstraint],
                     version: int = 2) -> list[PlannedBatch]:
        """Batches (indices into ``constraints``) and their estimated tokens, exactly as ``reframe`` will send them."""
        views = [self._reframe_view(0, c) for c in constraints]
        return self._pack("reframe", ReframeOutput, {"product_category": product_category, "constraints_json": []},
                          views, lambda v: self._item_tokens(v) + 70, version=version)

    @staticmethod
    def _doc_classify_view(j: int, c: BuyerConstraint) -> dict:
        return {k: v for k, v in {
            "idx": j, "attribute": c.attribute, "spec_name": c.spec_name, "operator": c.operator,
            "value": c.buyer_value, "category": c.constraint_category}.items() if v is not None}

    @property
    def rules_on(self) -> bool:
        """D066 deterministic-first resolution. ``llm.deterministic_first`` (default true) false = LLM for everything
        (tests of the LLM path, A/B comparison)."""
        return bool(self._llm.get("deterministic_first", True))

    def doc_classify_split(self, constraints: list[BuyerConstraint]) -> tuple[dict[int, str], list[int]]:
        """D066: ``({index: doc_name}`` resolved by the keyword table, ``[indices left for the LLM])``. No network."""
        by_rule: dict[int, str] = {}
        todo: list[int] = []
        for g, c in enumerate(constraints):
            if not self.rules_on:
                todo.append(g)
                continue
            hit = rules.classify_doc_proof(c.attribute, c.spec_name, c.operator, c.buyer_value, c.unit)
            if hit is not None and hit[0]:
                by_rule[g] = hit[1]
            else:
                todo.append(g)
        return by_rule, todo

    def plan_doc_classify(self, constraints: list[BuyerConstraint]) -> list[PlannedBatch]:
        """Indices refer to ``constraints``; rule-resolved items are not sent (executed == planned)."""
        _, todo = self.doc_classify_split(constraints)
        views = [self._doc_classify_view(0, constraints[g]) for g in todo]
        packed = self._pack("doc_classify", DocClassifyOutput, {"constraints_json": []}, views,
                            lambda v: self._COMP_DOC_CLASSIFY)
        return [PlannedBatch(tuple(todo[i] for i in pb.indices), pb.est_input_tokens, pb.est_completion_tokens,
                             pb.oversize) for pb in packed]

    @staticmethod
    def align_unique(constraints: list[BuyerConstraint]) -> list[BuyerConstraint]:
        """The constraints ``align`` actually sends: first occurrence of each buyer attribute."""
        seen: set[str] = set()
        return [c for c in constraints if not (c.attribute in seen or seen.add(c.attribute))]

    def _align_candidates(self, constraints: list[BuyerConstraint], candidate_constraints: list[CandidateConstraint],
                          max_candidate_attrs: int) -> tuple[list[str], set[str]]:
        freq: dict[str, int] = {}
        label: dict[str, str] = {}
        for cc in candidate_constraints:
            freq[cc.attribute] = freq.get(cc.attribute, 0) + 1
            label.setdefault(cc.attribute, cc.spec_name or "")
        # Budget (D024): keep the list short. Catalogue attributes sharing a word with any buyer
        # attribute/spec name come first, then the most frequent ones.
        words = {w for c in constraints for w in re.split(r"[^a-z0-9]+", f"{c.attribute} {c.spec_name or ''}".lower())
                 if len(w) > 3}
        related = lambda a: any(w in words for w in re.split(r"[^a-z0-9]+", f"{a} {label[a]}".lower()))  # noqa: E731
        top = sorted(freq, key=lambda a: (not related(a), -freq[a], a))[:max_candidate_attrs]
        return [f"{a}|{label[a]}" for a in top], set(top)

    @staticmethod
    def _align_view(c: BuyerConstraint) -> dict:
        return {k: v for k, v in {"buyer_attribute": c.attribute, "spec_name": c.spec_name,
                                  "value": c.buyer_value, "unit": c.unit}.items() if v is not None}

    def align_split(self, constraints: list[BuyerConstraint], candidate_constraints: list[CandidateConstraint],
                    ) -> tuple[dict[int, list[tuple[str, float]]], list[int]]:
        """D066: ``({index into align_unique(constraints): [(candidate_attribute, confidence)]}`` from the name matcher,
        ``[indices left for the LLM])``.  No network."""
        labels: dict[str, str] = {}
        for cc in candidate_constraints:
            labels.setdefault(cc.attribute, cc.spec_name or "")
        pairs = sorted(labels.items())
        by_rule: dict[int, list[tuple[str, float]]] = {}
        todo: list[int] = []
        for g, c in enumerate(self.align_unique(constraints)):
            m = rules.match_align(c.attribute, c.spec_name, pairs) if self.rules_on else []
            if m:
                by_rule[g] = m
            else:
                todo.append(g)
        return by_rule, todo

    def plan_align(self, constraints: list[BuyerConstraint], candidate_constraints: list[CandidateConstraint],
                   max_candidate_attrs: int = 120) -> list[PlannedBatch]:
        """Indices refer to ``align_unique(constraints)``; rule-resolved attributes are not sent."""
        uniq = self.align_unique(constraints)
        _, todo = self.align_split(constraints, candidate_constraints)
        rest = [uniq[g] for g in todo]
        cand_list, _ = self._align_candidates(rest, candidate_constraints, max_candidate_attrs)
        views = [self._align_view(c) for c in rest]
        packed = self._pack("align", AlignOutput, {"buyer_json": [], "candidate_attrs_json": cand_list}, views,
                            lambda v: self._COMP_ALIGN)
        return [PlannedBatch(tuple(todo[i] for i in pb.indices), pb.est_input_tokens, pb.est_completion_tokens,
                             pb.oversize) for pb in packed]

    @staticmethod
    def _normalise_view(g: int, r: "NormaliseRequest") -> dict:
        return {"item_id": g, "text": r.text, "target": r.target, "canonical_unit": r.canonical_unit}

    def plan_normalise(self, requests: list["NormaliseRequest"]) -> list[PlannedBatch]:
        views = [self._normalise_view(g, r) for g, r in enumerate(requests)]
        return self._pack("normalise", NormaliseOutput, {"items_json": []}, views, lambda v: self._COMP_NORMALISE)

    def doc_claims_split(self, requests: list[DocClaimRequest]) -> tuple[dict[int, str], list[int], list[int]]:
        """D066: ``({index: rule quote}, [prefiltered indices (no claim)], [indices left for the LLM])``. No network."""
        quotes: dict[int, str] = {}
        pre: list[int] = []
        todo: list[int] = []
        for g, r in enumerate(requests):
            if not maybe_claims(r.listing_text, r.doc_name, self.settings):
                pre.append(g)
                continue
            q = rules.find_claim_quote(r.listing_text, r.doc_name) if self.rules_on else None
            if q is not None and verify_quote(q, r.listing_text):
                quotes[g] = q
            else:
                todo.append(g)
        return quotes, pre, todo

    def rule_split(self, runner: str, *args: Any, **kwargs: Any) -> dict[str, int]:
        """Offline (no network, no client call) count of how many items of a prep runner are resolved by the
        deterministic rules vs left for the LLM, for the SAME arguments the runner takes (minus seed/ctx):

            runners.rule_split("doc_classify", constraints)
            runners.rule_split("align", constraints, candidate_constraints)
            runners.rule_split("doc_claims", requests)

        Returns ``{"n_items", "n_rule", "n_prefilter", "n_llm"}`` (n_prefilter is 0 except for doc_claims; for align
        ``n_items`` counts unique buyer attributes).  ``plan(runner, ...)`` already excludes the rule items, so
        ``len(plan)`` is the HTTP-call count of what will really be sent."""
        if runner == "doc_classify":
            rl, todo = self.doc_classify_split(*args, **kwargs)
            return {"n_items": len(rl) + len(todo), "n_rule": len(rl), "n_prefilter": 0, "n_llm": len(todo)}
        if runner == "align":
            rl, todo = self.align_split(*args, **kwargs)
            return {"n_items": len(rl) + len(todo), "n_rule": len(rl), "n_prefilter": 0, "n_llm": len(todo)}
        if runner == "doc_claims":
            rl, pre, todo = self.doc_claims_split(*args, **kwargs)
            return {"n_items": len(rl) + len(pre) + len(todo), "n_rule": len(rl), "n_prefilter": len(pre),
                    "n_llm": len(todo)}
        raise KeyError(f"no rule split for runner {runner!r} (reframe, normalise and the dialogue prompts are LLM-only)")

    def _claim_view(self, g: int, r: DocClaimRequest) -> dict:
        """D068: the listing text sent is capped at doc_claims.max_listing_chars (a quote must come from what was sent)."""
        return {"item_id": g, "doc_name": r.doc_name, "listing_text": r.listing_text[:self._max_listing]}

    def plan_doc_claims(self, requests: list[DocClaimRequest]) -> list[PlannedBatch]:
        """Indices refer to ``requests`` (items removed by the term-overlap prefilter or resolved by the rule are not sent)."""
        _, _, todo = self.doc_claims_split(requests)
        views = [self._claim_view(g, requests[g]) for g in todo]
        packed = self._pack("doc_claim", DocClaimOutput, {"items_json": []}, views, lambda v: self._COMP_DOC_CLAIM,
                            max_items=int(self.settings["doc_claims"]["batch_size"]), version=2)
        return [PlannedBatch(tuple(todo[i] for i in pb.indices), pb.est_input_tokens, pb.est_completion_tokens,
                             pb.oversize) for pb in packed]

    def plan(self, runner: str, *args: Any, **kwargs: Any) -> list[PlannedBatch]:
        """No-network plan for ``runner`` in {reframe, doc_classify, align, normalise, doc_claims} with the same
        arguments the runner takes (minus seed/ctx).  Use ``len(plan)`` as the HTTP-call count and
        ``sum(pb.est_input_tokens + pb.est_completion_tokens)`` as the token count (before validation retries)."""
        fn = {"reframe": self.plan_reframe, "doc_classify": self.plan_doc_classify, "align": self.plan_align,
              "normalise": self.plan_normalise, "doc_claims": self.plan_doc_claims}.get(runner)
        if fn is None:
            raise KeyError(f"no batch plan for runner {runner!r}")
        return fn(*args, **kwargs)

    # ------------------------------------------------------------ reframe
    @staticmethod
    def _reframe_view(i: int, c: BuyerConstraint) -> dict:
        av = [a for a in (c.allowed_values or []) if a != "None"]
        return {k: v for k, v in {
            "idx": i, "golden_param": c.golden_param, "attribute": c.attribute, "spec_name": c.spec_name,
            "operator": c.operator, "value": c.buyer_value, "unit": c.unit, "allowed_values": av or None,
            "condition": c.condition, "category": c.constraint_category,
        }.items() if v is not None}

    def reframe(self, product_category: str, constraints: list[BuyerConstraint], seed: Optional[int] = None,
                ctx: Optional[ConversationContext] = None, version: int = 2) -> LLMResult[list[BuyerConstraint]]:
        out = list(constraints)
        res: LLMResult[list[BuyerConstraint]] = LLMResult(out, extra={"reasons": {}})
        plan = self.plan_reframe(product_category, constraints, version)
        for b, pb in enumerate(plan):
            batch = list(pb.indices)
            view = [self._reframe_view(j, constraints[g]) for j, g in enumerate(batch)]

            def check(o: ReframeOutput, n=len(batch)) -> Optional[str]:
                idxs = [it.idx for it in o.items]
                if sorted(idxs) != list(range(n)):
                    return f"items must contain each idx 0..{n - 1} exactly once"
                for it in o.items:
                    hit = utterance_problem(it.commercial_text, self.terms) if it.commercial_text else None
                    if hit:
                        return f"item idx {it.idx} commercial_text: {hit}"
                    if it.framing_decision in ("keep", "translate"):
                        miss = reframe_value_problem(constraints[batch[it.idx]], it.commercial_text)
                        if miss:
                            return f"item idx {it.idx} commercial_text: {miss}"
                return None

            parsed, ids, err = self._call(
                "reframe", {"product_category": product_category, "constraints_json": view},
                ReframeOutput, check, seed, version=version, ctx=ctx, batch_info=pb.info("reframe", b, len(plan)))
            res.llm_call_ids += ids
            if parsed is None:
                res.qa_flags.append(self._flag("reframe", err or "", f"batch{b}"))
                continue
            for it in parsed.items:
                c = constraints[batch[it.idx]]
                mism = [n for n, a, bb in (
                    ("golden_param", it.golden_param, c.golden_param), ("operator", it.operator, c.operator),
                    ("value", it.value, c.buyer_value), ("unit", it.unit, c.unit)) if not _same(a, bb)]
                if mism == ["value"]:
                    # Only the echoed value text was reworded: the framing decision / commercial_text / reason are
                    # still the model's judgement and are kept, but the structured value, buyer_value, unit,
                    # operator and golden_param stay ORIGINAL (rule 1; a model-altered value is never taken).
                    # Any translation must be carried by commercial_text. Flag is distinct from reframe_mismatch.
                    res.qa_flags.append(f"reframe_value_reworded:{c.constraint_id}")
                elif mism:
                    res.qa_flags.append(f"reframe_mismatch:{c.constraint_id}:{','.join(mism)}")
                    continue
                drop = it.framing_decision == "drop"
                out[batch[it.idx]] = c.model_copy(update={
                    "framing_decision": FramingDecision(it.framing_decision),
                    "commercial_text": None if drop else it.commercial_text.strip(),
                    "framing_reason": it.reason.strip(),
                    "llm_call_ids": c.llm_call_ids + [ids[-1]],
                })
                res.extra["reasons"][c.constraint_id] = it.reason
        return res

    # ------------------------------------------------------------ doc_classify
    def doc_classify(self, constraints: list[BuyerConstraint], seed: Optional[int] = None,
                     ctx: Optional[ConversationContext] = None) -> LLMResult[list[BuyerConstraint]]:
        out = list(constraints)
        res: LLMResult[list[BuyerConstraint]] = LLMResult(out)
        by_rule, _ = self.doc_classify_split(constraints)
        for g, doc in by_rule.items():
            c = constraints[g]
            out[g] = c.model_copy(update={"is_doc_proof": True, "doc_name": doc,
                                          "llm_call_ids": c.llm_call_ids + [rules.RULE_DOC_CLASSIFY]})
            self._note_path(res, c.constraint_id, "rule")
        plan = self.plan_doc_classify(constraints)
        for b, pb in enumerate(plan):
            batch = list(pb.indices)
            view = [self._doc_classify_view(j, constraints[g]) for j, g in enumerate(batch)]

            def check(o: DocClassifyOutput, n=len(batch)) -> Optional[str]:
                if sorted(it.idx for it in o.items) != list(range(n)):
                    return f"items must contain each idx 0..{n - 1} exactly once"
                return None

            parsed, ids, err = self._call("doc_classify", {"constraints_json": view}, DocClassifyOutput, check, seed, ctx=ctx,
                                          batch_info=pb.info("doc_classify", b, len(plan)))
            res.llm_call_ids += ids
            if parsed is None:
                res.qa_flags.append(self._flag("doc_classify", err or "", f"batch{b}"))
                continue
            for it in parsed.items:
                c = constraints[batch[it.idx]]
                name = _norm(it.doc_name)
                if it.is_doc_proof and name is None:
                    res.qa_flags.append(f"doc_classify_missing_doc_name:{c.constraint_id}")
                    continue
                out[batch[it.idx]] = c.model_copy(update={
                    "is_doc_proof": it.is_doc_proof, "doc_name": name if it.is_doc_proof else None,
                    "llm_call_ids": c.llm_call_ids + [ids[-1]],
                })
                self._note_path(res, c.constraint_id, self._llm_path(ids[-1]))
        self._log_paths("doc_classify", res, ctx)
        return res

    # ------------------------------------------------------------ persona
    def persona(
        self, product_category: str, quantity: float, quantity_unit: str, budget_inr: Optional[float],
        categories: list[str], scenario_seed: int, scenario_id: str = "",
        tau_band: Optional[tuple[float, float]] | str = "auto",
        bands: Optional[dict] = None, tightness_target: Optional[int] = None,
        ctx: Optional[ConversationContext] = None,
        strata: Optional[dict] = None, avoid_profiles: Optional[list[dict]] = None, nonce_bump: int = 0,
    ) -> LLMResult[Optional[tuple[BuyerProfile, Persona]]]:
        """Invent the buyer company + persona.

        D060: ``strata`` (from ``gtgen.llm.strata.persona_strata``) replaces the tau-band logic: the prompt (persona v4,
        used when ``bands`` and ``strata`` are both given) states every trait band, every LLM-chosen value must lie
        inside its band (else the usual retry with the error appended) and the strata are stored in ``Persona.strata``;
        ``tau_band`` is then the tau stratum band. ``avoid_profiles`` = companies already used in the run (listed in the
        prompt to avoid); ``nonce_bump`` > 0 changes the nonce for a deterministic regeneration (see ``persona_unique``).
        """
        cats = with_location(categories, self.settings) if bands is not None else sorted({c for c in categories if c})   # D076: LOCATION has its own tolerance entry
        strata = strata or None
        if strata and "tau" in strata:
            tau_band = (float(strata["tau"]["band"][0]), float(strata["tau"]["band"][1]))
        if bands is None and getattr(self.client, "strict", False):
            raise ValueError("persona: live mode needs the code-derived commercial-term bands (persona v3, D054)")
        if tau_band == "auto":      # LLMs cluster tau (observed 0.18/0.18/0.19): stratify by seed, LLM picks inside
            tau_band = self.tau_band_for_seed(scenario_seed)
        nonce = f"{scenario_seed}-{hashlib.sha1(f'{scenario_id}:{scenario_seed}'.encode()).hexdigest()[:8]}"
        if nonce_bump:
            nonce += f"-r{int(nonce_bump)}"
        variables = {
            "product_category": product_category, "quantity": quantity, "quantity_unit": quantity_unit,
            "budget_inr": budget_inr, "categories_json": cats, "weight_keys_json": list(self.weight_keys),
            "nonce": nonce, "tau_guidance": self._tau_guidance(tau_band),
        }

        terms_spec: dict = {}
        if bands is not None:
            terms_spec = {k: v for k, v in dict(bands.get("terms", {})).items() if k not in _RANKING_ONLY_TERMS}
            unknown = sorted(set(terms_spec) - set(_KNOWN_TERMS))
            if unknown:
                raise ValueError(f"bands contain unknown commercial terms {unknown}")
            variables["bands_json"] = {"terms": terms_spec, "omitted": {k: v for k, v in (bands.get("omitted") or {}).items()
                                                                          if k not in _RANKING_ONLY_TERMS}}
            variables["tightness_hint"] = tightness_hint(tightness_target)
            ranges = self._persona_ranges()
            variables["ranges_json"] = {k: list(v) for k, v in ranges.items()}   # persona v3 (D053/D056)
            if strata:
                variables["strata_guidance"] = self._strata_guidance(strata)
                variables["avoid_guidance"] = self._avoid_guidance(avoid_profiles)

        def check(o: PersonaOutput) -> Optional[str]:
            if bands is not None:
                bad = self._picks_problem(self._clean_picks(o.commercial_terms), terms_spec)  # type: ignore[attr-defined]
                if bad:
                    return bad
            got = [t.category for t in o.category_tolerance]
            missing = [c for c in cats if c not in got]
            if missing:
                return f"category_tolerance is missing categories {missing}; give exactly one entry per category"
            if len(set(got)) != len(got):
                return "category_tolerance has duplicate categories"
            if tau_band is not None and not (tau_band[0] <= o.tau <= tau_band[1]):
                return f"tau {o.tau} is outside the required range {tau_band[0]:.2f} to {tau_band[1]:.2f}"
            if strata and bands is not None:
                bad = self._strata_problem(o, strata)
                if bad:
                    return bad
            if bands is not None:
                tol = [t.category for t in o.tolerance]    # type: ignore[attr-defined]
                if sorted(set(cats) - set(tol)) or len(set(tol)) != len(tol):
                    return f"tolerance must have exactly one entry per category {cats}"
                sp = o.sales_persona                       # type: ignore[attr-defined]
                text = f"{sp.speaking_style} {sp.sales_approach}"
                problem = utterance_problem(text, self.terms)
                if problem:
                    return f"sales_persona: {problem}"
                try:
                    assert_negotiator_safe(sp.model_dump())
                except NegotiatorInputError as exc:
                    return f"sales_persona: {exc}"
            return None

        parsed, ids, err = (
            self._call("persona", variables, PersonaOutput, check, seed=scenario_seed, ctx=ctx) if bands is None else
            self._call("persona", variables, PersonaOutputV3, check, seed=scenario_seed,
                       version=8 if strata else 3, ctx=ctx))
        if parsed is None:
            return LLMResult(None, ids, [self._flag("persona", err or "", f"seed{scenario_seed}")])
        cid = ids[-1]
        w, weights_reordered = self.order_weights(parsed.ranking_weights.model_dump())
        total = sum(w.values())
        tau = round(parsed.tau, 3)
        style = parsed.style.strip()
        extra: dict[str, Any] = {"weights_reordered": weights_reordered, "max_relaxations_clamped": None}
        relax: dict[str, Any] = {}
        if bands is not None:
            v = parsed.voice                                  # type: ignore[attr-defined]
            style = (f"{style} Formality: {v.formality.strip()}. Verbosity: {v.verbosity.strip()}. "
                     f"Regional voice: {v.regional_voice.strip()}.")
            relax, clamped = self._clamp_relaxation(parsed, cats)
            relax, tier_clamp = self._clamp_by_tier(relax, tier_for_tau(tau, self.settings))
            extra["max_relaxations_clamped"] = tier_clamp
            relax, extra["location_tolerance_scaled"] = self._scale_location(relax, tier_for_tau(tau, self.settings))
            ls = extra["location_tolerance_scaled"]
            lc = str(self.settings["relaxation"]["location"].get("persona_category", "LOCATION"))
            if ls and strata and lc in (strata.get("tolerance") or {}):   # stored stratum must still contain the used value
                tb = strata["tolerance"][lc]["band"]
                strata = {**strata, "tolerance": {**strata["tolerance"], lc: {**strata["tolerance"][lc],
                          "band": [min(tb[0], ls["to"]), tb[1]]}}}
            if tier_clamp and strata and "max_relaxations" in strata:   # stored stratum must still contain the used value
                strata = {**strata, "max_relaxations": {**strata["max_relaxations"], "band": [
                    min(strata["max_relaxations"]["band"][0], tier_clamp["to"]),
                    max(strata["max_relaxations"]["band"][1], tier_clamp["to"])]}}
            sp = parsed.sales_persona                         # type: ignore[attr-defined]
            extra.update({"voice": v.model_dump(), "sales_persona": sp.model_dump(), "clamped": clamped})
        profile = BuyerProfile(company_name=parsed.company_name.strip(), industry=parsed.industry.strip(),
                               city=parsed.city.strip(), style=style, llm_call_id=cid)
        persona = Persona(
            persona_seed=scenario_seed, tau=tau, tau_band=tuple(tau_band) if tau_band else None,
            tier=tier_for_tau(tau, self.settings),
            category_tolerance={t.category: t.multiplier for t in parsed.category_tolerance if t.category in cats},
            counter_band=parsed.counter_band, patience_turns=parsed.patience_turns,
            max_concessions_per_turn=parsed.max_concessions_per_turn,
            ranking_weights={k: w[k] / total for k in self.weight_keys},
            commercial_terms=self._clean_picks(parsed.commercial_terms) if bands is not None else {},  # type: ignore[attr-defined]
            strata=strata or {}, llm_call_id=cid, **relax)
        return LLMResult((profile, persona), ids, extra=extra)

    @staticmethod
    def _fmt_band(band: list) -> str:
        lo, hi = band
        return f"{lo} to {hi}" if isinstance(lo, int) else f"{lo:.3f} to {hi:.3f}"

    def _strata_guidance(self, strata: dict) -> str:
        """D060: the code-assigned band of every stratified trait, in words (no stratum indices)."""
        lines = []
        for tr in ("tau", "max_relaxations", "patience_rounds", "decision_randomness"):
            if tr in strata:
                lines.append(f"- {tr}: a value from {self._fmt_band(strata[tr]['band'])} (inclusive)")
        tol = strata.get("tolerance") or {}
        if tol:
            lines.append("- tolerance (value per category): " + "; ".join(
                f"{c}: {self._fmt_band(s['band'])}" for c, s in sorted(tol.items())))
        return "\n".join(lines)

    @staticmethod
    def _avoid_guidance(avoid: Optional[list[dict]]) -> str:
        if not avoid:
            return "No other company has been created in this batch yet."
        rows = "; ".join(f"{a.get('company_name', '')} ({a.get('industry', '')}, {a.get('city', '')})"
                         for a in avoid if a.get("company_name")) or "none"
        text = ("Companies already created in this batch (company name, industry, city). Do NOT reuse or closely "
                f"imitate these names, and do not repeat an industry and city pair: {rows}")
        sales = [a for a in avoid if a.get("sales_style") or a.get("sales_approach")]
        if sales:                                   # D072/D078: sales personas (style/approach only) must differ across the run
            srows = "; ".join(f"{a.get('sales_style', '')} / {a.get('sales_approach', '')}" for a in sales)
            text += ("\nSales negotiators already used in this batch (style / approach). The new sales_persona must have a "
                     f"clearly different speaking_style and a different sales_approach: {srows}")
        return text

    @staticmethod
    def _strata_problem(o: Any, strata: dict) -> Optional[str]:
        """D060: every LLM-chosen trait value must lie inside its stratum band (tau is checked by the caller)."""
        for tr in ("max_relaxations", "patience_rounds", "decision_randomness"):
            if tr in strata:
                lo, hi = strata[tr]["band"]
                v = getattr(o, tr)
                if not (lo <= v <= hi):
                    return f"{tr} {v} is outside the required range {lo} to {hi}"
        for t in o.tolerance:
            s = (strata.get("tolerance") or {}).get(t.category)
            if s and not (s["band"][0] <= t.value <= s["band"][1]):
                return (f"tolerance for {t.category} is {t.value}, outside the required range "
                        f"{s['band'][0]} to {s['band'][1]}")
        return None

    def _persona_ranges(self) -> dict[str, tuple[float, float]]:
        try:
            raw = self.settings["relaxation"]["persona_ranges"]
            return {k: (float(raw[k][0]), float(raw[k][1]))
                    for k in ("max_relaxations", "patience_rounds", "decision_randomness", "tolerance")}
        except KeyError as exc:
            raise KeyError(f"settings relaxation.persona_ranges is missing {exc} (persona v3, D053)") from exc

    def _scale_location(self, relax: dict, tier: Any) -> tuple[dict, Optional[dict]]:
        """D076: STRICT personas resist location harder: multiply their LOCATION tolerance by
        relaxation.location.strict_resistance after tier binning. Returns (kwargs, {"from","to","factor"} or None)."""
        loc = (self.settings.get("relaxation", {}) or {}).get("location", {}) or {}
        cat = str(loc.get("persona_category", "LOCATION"))
        if not loc.get("enabled", False) or getattr(tier, "value", tier) != "strict" or cat not in relax["tolerance"]:
            return relax, None
        f = float(loc.get("strict_resistance", 0.5))
        cur = relax["tolerance"][cat]
        new = round(cur * f, 3)
        return {**relax, "tolerance": {**relax["tolerance"], cat: new}}, {"from": cur, "to": new, "factor": f}

    def _clamp_by_tier(self, relax: dict, tier: Any) -> tuple[dict, Optional[dict]]:
        """D070: clamp max_relaxations into relaxation.persona_ranges.max_relaxations_by_tier[tier] when present
        (after the tier is known from tau). Returns (kwargs, {"from": x, "to": y} or None)."""
        by = (self.settings.get("relaxation", {}).get("persona_ranges", {}) or {}).get("max_relaxations_by_tier") or {}
        rng = by.get(getattr(tier, "value", tier))
        if not rng:
            return relax, None
        cur = relax["max_relaxations"]
        new = int(min(max(cur, int(rng[0])), int(rng[1])))
        if new == cur:
            return relax, None
        return {**relax, "max_relaxations": new}, {"from": cur, "to": new}

    def _clamp_relaxation(self, parsed: Any, cats: list[str]) -> tuple[dict, dict]:
        """Clamp the model's suggestions to relaxation.persona_ranges. Returns (Persona kwargs, {field: [asked, used]})."""
        rg = self._persona_ranges()
        clamped: dict[str, Any] = {}

        def clamp(name: str, raw: float, as_int: bool) -> Any:
            lo, hi = rg[name.split(":")[0]]
            val = min(max(float(raw), lo), hi)
            val = int(round(val)) if as_int else round(val, 3)
            if val != raw:
                clamped[name] = [raw, val]
            return val

        kw = {
            "max_relaxations": clamp("max_relaxations", parsed.max_relaxations, True),
            "patience_rounds": clamp("patience_rounds", parsed.patience_rounds, True),
            "decision_randomness": clamp("decision_randomness", parsed.decision_randomness, False),
            "tolerance": {t.category: clamp(f"tolerance:{t.category}", t.value, False)
                          for t in parsed.tolerance if t.category in cats},
        }
        return kw, clamped

    def order_weights(self, w: dict) -> tuple[dict, bool]:
        """D070: the supplier-feature weights (settings ``ranking.supplier_priority``) are re-sorted so the first key
        holds the largest of their values, the next the next largest, ... (never an error). Returns (weights, reordered)."""
        keys = list(self.supplier_priority)
        vals = sorted((float(w[k]) for k in keys), reverse=True)
        out = dict(w)
        for k, v in zip(keys, vals):
            out[k] = v
        return out, any(out[k] != w[k] for k in keys)

    @staticmethod
    def _clean_picks(picks: Any) -> dict:
        d = picks.model_dump(exclude_none=True)
        for k in _RANKING_ONLY_TERMS:      # D070: ignored if an old prompt / model still returns them
            d.pop(k, None)
        if "preferred_states" in d:
            d["preferred_states"] = list(dict.fromkeys(d["preferred_states"]))
        return d

    @staticmethod
    def _picks_problem(picks: dict, terms: dict) -> Optional[str]:
        """Runner-level validation of commercial-term picks against the code-derived bands (D049)."""
        if set(picks) != set(terms):
            miss, extra = sorted(set(terms) - set(picks)), sorted(set(picks) - set(terms))
            return f"commercial_terms must contain exactly the allowed terms (missing {miss}, not allowed {extra})"
        for name, spec in terms.items():
            v = picks[name]
            if spec.get("kind") == "set":
                opts = set(spec["options"])
                uniq = list(dict.fromkeys(v))
                if not set(uniq) <= opts:
                    return f"{name}: values {sorted(set(uniq) - opts)} are not in the allowed options"
                if not int(spec.get("min_k", 1)) <= len(uniq) <= int(spec["max_k"]):
                    return f"{name}: pick between {spec.get('min_k', 1)} and {spec['max_k']} distinct options (got {len(uniq)})"
            else:
                if not (spec["lo"] - 1e-9 <= v <= spec["hi"] + 1e-9):
                    return f"{name}: {v} is outside the allowed range {spec['lo']} to {spec['hi']}"
        return None

    def tau_band_for_seed(self, seed: int) -> tuple[float, float]:
        """Deterministic stratification: the seed selects a tier; the band is that tier's interior (settings tiers)."""
        bounds = sorted(self.settings["persona"]["tiers"].values())
        lows = [0.0] + bounds[:-1]
        highs = [min(b, 1.0) for b in bounds]
        k = seed % len(bounds)
        lo, hi = lows[k], highs[k]
        pad = 0.01 if k else 0.05
        return (round(lo + (0.05 if k == 0 else pad), 3), round(hi - 0.01, 3))

    @staticmethod
    def _tau_guidance(band: Optional[tuple[float, float]]) -> str:
        if band is None:
            return ("Buyers differ widely: roughly a quarter are easygoing (tau below 0.25), a quarter careful "
                    "(0.25 to 0.50), a quarter firm (0.50 to 0.75) and a quarter very strict (above 0.75).")
        return (f"Choose tau between {band[0]:.2f} and {band[1]:.2f} for this company (and make the personality "
                "consistent with that).")

    # ------------------------------------------------------------ buyer utterances
    @staticmethod
    def _profile_view(p: BuyerProfile) -> dict:
        return {"company_name": p.company_name, "industry": p.industry, "city": p.city, "style": p.style}

    @staticmethod
    def _transcript_view(t: TranscriptView, last_n: Optional[int] = None) -> list[dict]:
        return _transcript_dicts(t, last_n)

    def _utterance_check(self, o: Any) -> Optional[str]:
        return _speech_problem(o.utterance, self.terms)

    def buyer_behaviour(self, persona: Optional[Persona]) -> str:
        """Behaviour words derived from tier and traits; never numbers, tiers or thresholds (D054)."""
        if persona is None:
            if getattr(self.client, "strict", False):
                raise ValueError("buyer utterances need the Persona in live mode (behaviour words, D054)")
            return "no particular negotiating trait"
        parts = [BEHAVIOUR_BY_TIER[persona.tier.value]]
        if persona.patience_turns <= 4:
            parts.append("short on time and quick to lose patience")
        elif persona.patience_turns >= 9:
            parts.append("patient and willing to talk things through")
        if persona.max_concessions_per_turn <= 1:
            parts.append("likes to settle one point at a time")
        return "; ".join(parts)

    @staticmethod
    def _voice_nonce(ctx: Optional[ConversationContext]) -> str:
        if ctx is None:
            return "none"
        h = hashlib.sha1(f"{ctx.scenario_id}:{ctx.cache_seed}:{ctx.turn_idx}".encode()).hexdigest()[:8]
        return f"{ctx.cache_seed}-{ctx.turn_idx if ctx.turn_idx is not None else 'x'}-{h}"

    @staticmethod
    def _last_message(t: TranscriptView, speaker: str) -> str:
        for turn in reversed(t.turns):
            if turn.speaker.value == speaker:
                return turn.utterance
        return ""

    def _buyer_numbers_check(self, sources: list[Any]) -> Callable[[Any], Optional[str]]:
        def check(o: Any) -> Optional[str]:
            return self._utterance_check(o) or numbers_problem(o.utterance, sources)
        return check

    def buyer_opening(
        self, profile: BuyerProfile, product_category: str, quantity: float, quantity_unit: str,
        budget_inr: Optional[float], requirements: list[RequirementStatement], seed: Optional[int] = None,
        *, ctx: Optional[ConversationContext] = None, persona: Optional[Persona] = None,
    ) -> LLMResult[Optional[str]]:
        # D065: numbers reach the prompt already formatted ("500", "INR 2,70,562"), never as floats.
        qty_text, budget_text = format_quantity(quantity), format_inr(budget_inr)
        reqs = [{"text": tidy_numbers(r.text), "strength": r.strength} for r in requirements]
        variables = {
            "profile_json": self._profile_view(profile), "behaviour": self.buyer_behaviour(persona),
            "voice_nonce": self._voice_nonce(ctx), "product_category": product_category,
            "quantity": qty_text, "quantity_unit": quantity_unit, "budget_inr": budget_text,
            "requirements_json": reqs,
        }
        real = (self.settings.get("persona", {}) or {}).get("realism", {}) or {}
        lo_s, hi_s = int(real.get("opening_min_sentences", 2)), int(real.get("opening_max_sentences", 6))
        numbers_ok = self._buyer_numbers_check([quantity, qty_text, budget_inr, budget_text,
                                                [r.text for r in requirements], [r["text"] for r in reqs]])

        def check(o: UtteranceOutput) -> Optional[str]:
            return numbers_ok(o) or opening_problem(o.utterance, lo_s, hi_s)

        _, text, ids, flags = self._call_or_fallback(
            "buyer_opening", variables, UtteranceOutput, check, seed, 5, ctx,
            lambda: opening_fallback_text(product_category, qty_text, quantity_unit, budget_text, reqs))
        return LLMResult(text, ids, flags)

    def buyer_reply(
        self, profile: BuyerProfile, transcript: TranscriptView, turn_action: str,
        items: list[BuyerItemDecision], seed: Optional[int] = None,
        *, ctx: Optional[ConversationContext] = None, persona: Optional[Persona] = None,
        already_stated: Optional[list[str]] = None,
    ) -> LLMResult[Optional[str]]:
        """Verbalise decisions already made by the policy (YES/NO/BYPASS/COUNTER, and D055 PROOF_REQUEST)."""
        variables = {
            "profile_json": self._profile_view(profile), "behaviour": self.buyer_behaviour(persona),
            "voice_nonce": self._voice_nonce(ctx),
            "transcript_json": self._transcript_view(transcript, last_n=6),
            "last_negotiator_message": self._last_message(transcript, "NEGOTIATOR"),
            "turn_action": turn_action,
            "items_json": [{k: tidy_value(v) for k, v in i.model_dump(exclude_none=True).items()} for i in items],
            "already_stated_json": [tidy_numbers(a) for a in (already_stated or [])],
        }
        base = self._buyer_numbers_check([variables["items_json"], [i.model_dump(exclude_none=True) for i in items],
                                          variables["transcript_json"]])
        proof_docs = [i.doc_name for i in items if i.action == "PROOF_REQUEST" and i.doc_name]

        def check(o: UtteranceOutput) -> Optional[str]:
            bad = base(o) or no_greeting_problem(o.utterance)      # D078: greeting only in the opening, never a reply
            if bad:
                return bad
            low = o.utterance.lower()
            missing = [d for d in proof_docs if d.lower() not in low]
            if missing:
                return f"the proof request must name the document exactly: {missing}"
            counters = missing_counter_values(items, o.utterance)
            return f"state every counter_value exactly; missing: {counters}" if counters else None

        _, text, ids, flags = self._call_or_fallback(
            "buyer_reply", variables, UtteranceOutput, check, seed, 5, ctx,
            lambda: buyer_reply_fallback_text(items))
        return LLMResult(text, ids, flags)

    # ------------------------------------------------------------ negotiator (barrier-enforced)
    def negotiator_extract(self, transcript: TranscriptView, seed: Optional[int] = None,
                           ctx: Optional[ConversationContext] = None) -> LLMResult[list[BelievedConstraint]]:
        variables = build_negotiator_extract_variables(transcript)
        turn_idxs = {t.turn_idx for t in transcript.turns}

        def kept(o: ExtractOutput) -> list:
            return [i for i in o.items if not is_supplier_stat_attribute(i.attribute, i.value, i.unit)]

        def check(o: ExtractOutput) -> Optional[str]:
            bad = [i.source_turn for i in o.items if i.source_turn not in turn_idxs]
            if bad:
                return f"source_turn must be one of {sorted(turn_idxs)}; got {bad}"
            seen: dict[str, tuple] = {}
            for i in kept(o):
                key = i.attribute.strip().lower()
                sig = (_norm(i.operator), _norm(i.value), _norm(i.unit))
                if key in seen and seen[key] != sig:
                    return (f"attribute {i.attribute!r} is used for two different requirements; give each requirement its "
                            "own specific name built from the buyer's wording (or list a restated requirement once, "
                            "with its latest value)")
                seen[key] = sig
            return None

        parsed, ids, err = self._call("negotiator_extract", variables, ExtractOutput, check, seed, version=3, ctx=ctx)
        if parsed is None:
            return LLMResult([], ids, [self._flag("negotiator_extract", err or "")])
        dropped = len(parsed.items) - len(kept(parsed))
        seen_attr: set[tuple] = set()
        items = []
        for it in kept(parsed):       # identical duplicates collapse to the first
            sig = (it.attribute.strip().lower(), _norm(it.operator), _norm(it.value), _norm(it.unit))
            if sig not in seen_attr:
                seen_attr.add(sig)
                items.append(it)
        beliefs = [
            BelievedConstraint(
                belief_id=f"B{n + 1}", attribute=it.attribute.strip(), operator=_norm(it.operator),
                value=_norm(it.value), unit=_norm(it.unit), believed_hardness=Hardness(it.believed_hardness),
                is_doc_proof=it.is_doc_proof, source_turn=it.source_turn)
            for n, it in enumerate(items)]
        return LLMResult(beliefs, ids, extra={"dropped_supplier_stats": dropped})

    def _need_sales_persona(self, sales_persona: Optional[dict], what: str) -> None:
        if sales_persona is None and getattr(self.client, "strict", False):
            raise ValueError(f"{what}: live mode needs the scenario's sales persona (D054)")

    def negotiator_reply(
        self, transcript: TranscriptView, bundle: Optional[RelaxationBundle] = None,
        beliefs: Optional[list[BelievedConstraint]] = None, evidence: Optional[list[EvidenceSnippet]] = None,
        seed: Optional[int] = None, *, ctx: Optional[ConversationContext] = None,
        sales_persona: Optional[dict] = None, menu_point: Optional[MenuPointView] = None,
        allowed_numbers: Optional[list] = None,
    ) -> LLMResult[Optional[tuple[str, list[str]]]]:
        """PROPOSE a menu point (D053) or, legacy, a RelaxationBundle. Numbers are limited to ``allowed_numbers``,
        the menu values, numbers the buyer used and numbers in the supplied evidence."""
        evidence = evidence or []
        self._need_sales_persona(sales_persona, "negotiator_reply")
        variables = build_negotiator_reply_variables(
            transcript, bundle, beliefs, evidence, sales_persona=sales_persona, menu_point=menu_point,
            allowed_numbers=allowed_numbers, reply_kind="PROPOSE")
        ev_ids = {e.evidence_id for e in evidence}
        num_sources = [variables["allowed_numbers_json"], variables["transcript_json"],
                       [e.text for e in evidence]]

        def check(o: NegotiatorReplyOutput) -> Optional[str]:
            p = _speech_problem(o.utterance, self.terms) or no_greeting_problem(o.utterance)   # D078
            if p:
                return p
            bad = [i for i in o.cited_evidence_ids if i not in ev_ids]
            if bad:
                return f"cited_evidence_ids {bad} are not in the evidence"
            return numbers_problem(o.utterance, num_sources)

        parsed, text, ids, flags = self._call_or_fallback(
            "negotiator_reply", variables, NegotiatorReplyOutput, check, seed, 5, ctx,
            lambda: negotiator_propose_fallback_text(variables["menu_json"] or {}))
        cited = parsed.cited_evidence_ids if parsed is not None else []
        return LLMResult((text, cited), ids, flags)

    def negotiator_final(self, kind: str, facts: dict, sales_persona: Optional[dict] = None,
                         seed: Optional[int] = None, ctx: Optional[ConversationContext] = None) -> LLMResult[str]:
        """D069: LLM wording of the closing turn (kind "recommend" | "best_available"). Facts come from code
        (``facts = {"options": [{"rank", "product", "supplier", "price_text", "met", "unmet", "unverified"}],
        "to_confirm": [...], "note": ...}``); the model only phrases them. Numbers and names are checked against
        ``facts``. D078 amendment (user decision, 2026-11-01): the conversation must always end with a
        recommendation, so exhausted retries no longer raise ``ScenarioSkipped`` here - a deterministic fallback
        built from the same ``facts`` is used instead (see ``final_fallback_text``), flagged in qa_flags.
        ``sales_persona`` is the dict from ``scenario_meta['sales_persona']`` (name/speaking_style/sales_approach),
        rendered as one readable "Name: style. approach" line; a ready string is also accepted."""
        self._need_sales_persona(sales_persona, "negotiator_final")
        variables = build_negotiator_final_variables(kind, facts, sales_persona)
        facts = variables["facts_json"]          # scrubbed of supplier rating / years labels (D070)

        def check(o: NegotiatorFinalOutput) -> Optional[str]:
            return final_text_problem(o.utterance, facts, self.terms)

        _, text, ids, flags = self._call_or_fallback(
            "negotiator_final", variables, NegotiatorFinalOutput, check, seed, 4, ctx,
            lambda: final_fallback_text(kind, facts), max_retries=self.final_max_retries)
        return LLMResult(text, ids, flags)

    def negotiator_acknowledge(
        self, transcript: TranscriptView, proof_requests: list[ProofRequestView],
        evidence: list[EvidenceSnippet], seed: Optional[int] = None, *,
        ctx: Optional[ConversationContext] = None, sales_persona: Optional[dict] = None,
    ) -> LLMResult[Optional[tuple[str, list[str]]]]:
        """Action.ACKNOWLEDGE (D055): confirm the buyer's proof request(s) and name the document(s) the supplier must
        provide, from catalogue evidence only. The cited evidence must include each request's claim snippet."""
        self._need_sales_persona(sales_persona, "negotiator_acknowledge")
        variables = build_negotiator_reply_variables(
            transcript, None, None, evidence, sales_persona=sales_persona, reply_kind="ACKNOWLEDGE",
            proof_requests=proof_requests)
        ev_ids = {e.evidence_id for e in evidence}
        num_sources = [variables["allowed_numbers_json"], variables["transcript_json"], [e.text for e in evidence]]

        def check(o: NegotiatorReplyOutput) -> Optional[str]:
            p = _speech_problem(o.utterance, self.terms) or no_greeting_problem(o.utterance)   # D078
            if p:
                return p
            bad = [i for i in o.cited_evidence_ids if i not in ev_ids]
            if bad:
                return f"cited_evidence_ids {bad} are not in the evidence"
            low = o.utterance.lower()
            missing = [r.doc_name for r in proof_requests if r.doc_name.lower() not in low]
            if missing:
                return f"the message must name each required document exactly as given; missing {missing}"
            uncited = [r.evidence_id for r in proof_requests if r.evidence_id not in o.cited_evidence_ids]
            if uncited:
                return f"cited_evidence_ids must include the claim evidence {uncited}"
            return numbers_problem(o.utterance, num_sources)

        parsed, text, ids, flags = self._call_or_fallback(
            "negotiator_reply", variables, NegotiatorReplyOutput, check, seed, 5, ctx,
            lambda: negotiator_acknowledge_fallback_text(proof_requests))
        cited = parsed.cited_evidence_ids if parsed is not None else []
        return LLMResult((text, cited), ids, flags)

    def negotiator_clarify(
        self, transcript: TranscriptView, topics: list[str], known: list[str], seed: Optional[int] = None,
        *, ctx: Optional[ConversationContext] = None, sales_persona: Optional[dict] = None,
        already_asked: Optional[list[str]] = None,
    ) -> LLMResult[Optional[tuple[str, list[str]]]]:
        """D070: asks ONLY about ``topics`` minus ``already_asked`` (checked: asked_topics must be a non-empty subset of
        them while any remain; 'brand' is never asked unless a topic is a brand topic)."""
        self._need_sales_persona(sales_persona, "negotiator_clarify")
        variables = build_negotiator_clarify_variables(transcript, topics, known, sales_persona, already_asked)
        asked_before = set(variables["asked_json"])
        allowed = [t for t in variables["topics_json"] if t not in asked_before]
        topics_text = " ".join(allowed)
        off_topic = [(rx, why) for rx, why in _CLARIFY_OFF_TOPIC if not rx.search(topics_text)]
        used_openers = {_first_word(t.utterance) for t in transcript.turns if t.speaker.value == "NEGOTIATOR"}
        used_openers.discard("")

        def _resolve_asked(o: ClarifyOutput, allowed_: list[str]) -> tuple[list[str], list]:
            out: list[str] = []
            bad: list = []
            full = variables["topics_json"]            # ids are 0-based positions in the topic list as shown
            for i in o.asked_topic_ids:
                (out.append(full[i]) if 0 <= i < len(full) and full[i] in allowed_ else bad.append(i))
            for t in o.asked_topics:
                m = map_asked_topic(t, allowed_)
                (out.append(m) if m is not None else bad.append(t))
            return list(dict.fromkeys(out)), bad

        def check(o: ClarifyOutput) -> Optional[str]:
            # D078: the opener-repetition check (more specific, names the earlier message) takes priority over the
            # generic no-greeting check in _speech_problem when both would fire on the same greeting-word opener.
            opener = _first_word(o.utterance)
            if opener and opener in used_openers:
                return (f"your message starts with {opener!r}, which an earlier negotiator message already used; "
                        "start with different words (and use Namaste at most in the first message)")
            p = _speech_problem(o.utterance, self.terms) or no_greeting_problem(o.utterance)   # D078
            if p:
                return p
            resolved, bad = _resolve_asked(o, allowed)
            if bad:
                return (f"asked_topics {bad} are not allowed: ask only about topics in the topic list that were not "
                        "already asked, by id" + ("" if allowed else "; the list is empty, so return an empty list"))
            if allowed and not resolved:
                return "asked_topic_ids is empty: ask about at least one topic from the topic list and list its id in asked_topic_ids"
            if len(set(resolved)) > 2:
                return "ask about at most 2 topics in one message"
            for rx, why in off_topic:
                if rx.search(o.utterance):
                    return f"do not ask about {why}: it is not one of the topics you may ask about"
            return None

        parsed, ids, err = self._call("negotiator_clarify", variables, ClarifyOutput, check, seed, version=6, ctx=ctx)
        if parsed is None:
            return LLMResult(None, ids, [self._flag("negotiator_clarify", err or "")])
        return LLMResult((parsed.utterance.strip(), list(dict.fromkeys(_resolve_asked(parsed, allowed)[0]))), ids)

    def negotiator_interpret(
        self, transcript: TranscriptView, proposal_items: list[dict], seed: Optional[int] = None,
        ctx: Optional[ConversationContext] = None,
    ) -> LLMResult[Optional[list[InterpretItem]]]:
        variables = build_negotiator_interpret_variables(transcript, proposal_items)
        expected = sorted(i["index"] for i in variables["proposal_items_json"])
        if len(set(expected)) != len(expected):
            raise ValueError("proposal_items contain duplicate index values")

        def check(o: InterpretOutput) -> Optional[str]:
            got = sorted(i.index for i in o.items)
            if got != expected:
                return f"items must contain each index {expected} exactly once (got {got})"
            return None

        parsed, ids, err = self._call("negotiator_interpret", variables, InterpretOutput, check, seed, ctx=ctx)
        if parsed is None:
            return LLMResult(None, ids, [self._flag("negotiator_interpret", err or "")])
        return LLMResult(sorted(parsed.items, key=lambda i: i.index), ids)

    # ------------------------------------------------------------ buyer_reveal
    def buyer_reveal(
        self, profile: BuyerProfile, transcript: TranscriptView, requirements: list[RequirementStatement],
        last_question: str, seed: Optional[int] = None, *, ctx: Optional[ConversationContext] = None,
        persona: Optional[Persona] = None, already_stated: Optional[list[str]] = None,
    ) -> LLMResult[Optional[str]]:
        variables = {
            "profile_json": self._profile_view(profile), "behaviour": self.buyer_behaviour(persona),
            "voice_nonce": self._voice_nonce(ctx),
            "transcript_json": self._transcript_view(transcript, last_n=6),
            "requirements_json": [{"text": tidy_numbers(r.text), "strength": r.strength} for r in requirements],
            "already_stated_json": [tidy_numbers(a) for a in (already_stated or [])],
            "last_question": last_question,
        }
        numbers_ok = self._buyer_numbers_check([variables["requirements_json"], [r.text for r in requirements],
                                                variables["transcript_json"]])
        req_texts = [r.text for r in requirements] + [variables["requirements_json"][i]["text"] for i in range(len(requirements))]
        stated = [str(a) for a in (already_stated or [])]

        def check(o: UtteranceOutput) -> Optional[str]:
            return (numbers_ok(o) or reveal_scope_problem(o.utterance, req_texts, stated)
                    or reveal_answer_problem(o.utterance, [q["text"] for q in variables["requirements_json"]],
                                             last_question))

        _, text, ids, flags = self._call_or_fallback(
            "buyer_reveal", variables, UtteranceOutput, check, seed, 6, ctx,
            lambda: buyer_reveal_fallback_text(req_texts))
        return LLMResult(text, ids, flags)

    # ------------------------------------------------------------ align
    def align(
        self, bid_id: str, constraints: list[BuyerConstraint], candidate_constraints: list[CandidateConstraint],
        max_candidate_attrs: int = 120, seed: Optional[int] = None,
        ctx: Optional[ConversationContext] = None,
    ) -> LLMResult[list[AttributeAlignment]]:
        res: LLMResult[list[AttributeAlignment]] = LLMResult([])
        uniq = self.align_unique(constraints)
        by_rule, todo = self.align_split(constraints, candidate_constraints)
        for g, matches in by_rule.items():
            for cand_attr, conf in matches:
                res.value.append(AttributeAlignment(bid_id=bid_id, buyer_attribute=uniq[g].attribute,
                                                    candidate_attribute=cand_attr, confidence=conf,
                                                    llm_call_id=rules.RULE_ALIGN))
            self._note_path(res, uniq[g].attribute, "rule")
        cand_list, allowed = self._align_candidates([uniq[g] for g in todo], candidate_constraints, max_candidate_attrs)
        plan = self.plan_align(constraints, candidate_constraints, max_candidate_attrs)
        for b, pb in enumerate(plan):
            batch = [uniq[i] for i in pb.indices]
            view = [self._align_view(c) for c in batch]
            names = {c.attribute for c in batch}

            def check(o: AlignOutput) -> Optional[str]:
                for it in o.items:
                    if it.buyer_attribute not in names:
                        return f"buyer_attribute {it.buyer_attribute!r} is not one of the inputs"
                    for m in it.matches:
                        if m.candidate_attribute.split("|")[0] not in allowed:
                            return (f"candidate_attribute {m.candidate_attribute!r} is not in the catalogue list; "
                                    "copy the text before the '|' exactly")
                return None

            parsed, ids, err = self._call("align", {"buyer_json": view, "candidate_attrs_json": cand_list},
                                          AlignOutput, check, seed, ctx=ctx, batch_info=pb.info("align", b, len(plan)))
            res.llm_call_ids += ids
            if parsed is None:
                res.qa_flags.append(self._flag("align", err or "", f"batch{b}"))
                continue
            for it in parsed.items:
                for m in it.matches:
                    res.value.append(AttributeAlignment(
                        bid_id=bid_id, buyer_attribute=it.buyer_attribute,
                        candidate_attribute=m.candidate_attribute.split("|")[0],
                        confidence=m.confidence, llm_call_id=ids[-1]))
                self._note_path(res, it.buyer_attribute, self._llm_path(ids[-1]))
        self._log_paths("align", res, ctx)
        return res

    # ------------------------------------------------------------ normalise
    def normalise(self, requests: list[NormaliseRequest], seed: Optional[int] = None,
                  ctx: Optional[ConversationContext] = None) -> LLMResult[list[Optional[NormalisedValue]]]:
        out: list[Optional[NormalisedValue]] = [None] * len(requests)
        res: LLMResult[list[Optional[NormalisedValue]]] = LLMResult(out)
        plan = self.plan_normalise(requests)
        for b, pb in enumerate(plan):
            batch = list(pb.indices)
            view = [self._normalise_view(g, requests[g]) for g in batch]

            def check(o: NormaliseOutput, ids_=tuple(batch)) -> Optional[str]:
                if sorted(i.item_id for i in o.items) != list(ids_):
                    return f"items must contain each item_id {list(ids_)} exactly once"
                return None

            parsed, ids, err = self._call("normalise", {"items_json": view}, NormaliseOutput, check, seed, ctx=ctx,
                                          batch_info=pb.info("normalise", b, len(plan)))
            res.llm_call_ids += ids
            if parsed is None:
                res.qa_flags.append(self._flag("normalise", err or "", f"batch{b}"))
                continue
            for it in parsed.items:
                v = _norm(it.value)
                if v is None:
                    continue
                out[it.item_id] = NormalisedValue(
                    source_text=requests[it.item_id].text, value=v, unit=_norm(it.unit),
                    confidence=it.confidence, llm_call_id=ids[-1])
        return res

    # ------------------------------------------------------------ doc_claims (D051)
    def doc_claims(self, requests: list[DocClaimRequest], seed: Optional[int] = None,
                   ctx: Optional[ConversationContext] = None) -> LLMResult[list[DocClaim]]:
        """Aligned to ``requests``. The LLM quotes the listing; code verifies the quote (unverified = no claim)."""
        for r in requests:
            if not isinstance(r, DocClaimRequest):
                raise TypeError("doc_claims accepts only DocClaimRequest items")
        cfg = self.settings["doc_claims"]
        batch_size = int(cfg["batch_size"])

        def no_claim(r: DocClaimRequest, cid: str, quote: Optional[str] = None) -> DocClaim:
            return DocClaim(product_id=r.product_id, constraint_ref=r.constraint_ref, claims=False, quote=quote,
                            quote_verified=False, llm_call_id=cid)

        out: list[Optional[DocClaim]] = [None] * len(requests)
        res: LLMResult[list[DocClaim]] = LLMResult([])
        quotes, pre, _todo = self.doc_claims_split(requests)
        key = lambda r: f"{r.product_id}|{r.constraint_ref}"   # noqa: E731
        for g in pre:
            out[g] = no_claim(requests[g], "prefilter:no_term_overlap")
            self._note_path(res, key(requests[g]), "prefilter")
        for g, q in quotes.items():
            r = requests[g]
            out[g] = DocClaim(product_id=r.product_id, constraint_ref=r.constraint_ref, claims=True, quote=q,
                              quote_verified=True, llm_call_id=rules.RULE_DOC_CLAIM)
            self._note_path(res, key(r), "rule")
        plan = self.plan_doc_claims(requests)
        for b, pb in enumerate(plan):
            batch = list(pb.indices)
            view = [self._claim_view(g, requests[g]) for g in batch]
            assert_negotiator_safe(view)

            def check(o: DocClaimOutput, ids_=tuple(batch)) -> Optional[str]:
                if sorted(i.item_id for i in o.items) != list(ids_):
                    return f"items must contain each item_id {list(ids_)} exactly once"
                return None

            parsed, ids, err = self._call("doc_claim", {"items_json": view}, DocClaimOutput, check, seed, ctx=ctx,
                                          version=2, batch_info=pb.info("doc_claims", b, len(plan)))
            res.llm_call_ids += ids
            if parsed is None:
                res.qa_flags.append(self._flag("doc_claim", err or "", f"batch{b}"))
                for g in batch:
                    out[g] = no_claim(requests[g], "failed")
                continue
            for it in parsed.items:
                r = requests[it.item_id]
                if it.claims and it.quote:
                    ok = verify_quote(it.quote, r.listing_text[:self._max_listing])
                    cid = ids[-1]
                    if ok and not rules.claim_quote_supports(it.quote, r.doc_name):
                        # D073: the quote exists but is not about the required document -> no claim, marked, counted
                        ok, cid = False, f"claim_quote_mismatch:{ids[-1]}"
                        res.extra["claim_quote_mismatch"] = res.extra.get("claim_quote_mismatch", 0) + 1
                        res.extra.setdefault("claim_quote_mismatch_items", []).append(key(r))
                    out[it.item_id] = DocClaim(
                        product_id=r.product_id, constraint_ref=r.constraint_ref, claims=ok, quote=it.quote,
                        quote_verified=ok, llm_call_id=cid)
                else:
                    out[it.item_id] = no_claim(r, ids[-1])
                self._note_path(res, key(r), self._llm_path(ids[-1]))
        res.value = [d for d in out if d is not None]
        self._log_paths("doc_claims", res, ctx)
        return res

    # ------------------------------------------------------------ reveal_check / claim_check
    def reveal_check(self, utterance: str, constraints: list[BuyerConstraint], seed: Optional[int] = None,
                     ctx: Optional[ConversationContext] = None) -> LLMResult[Optional[list[str]]]:
        items = [{"id": c.constraint_id, "text": c.commercial_text or c.raw_text or c.attribute}
                 for c in constraints]
        valid = {c.constraint_id for c in constraints}

        def check(o: RevealOutput) -> Optional[str]:
            bad = [i for i in o.revealed_ids if i not in valid]
            return f"revealed_ids {bad} are not in the requirement list" if bad else None

        parsed, ids, err = self._call("reveal_check", {"utterance": utterance, "constraints_json": items},
                                      RevealOutput, check, seed, ctx=ctx)
        if parsed is None:
            return LLMResult(None, ids, [self._flag("reveal_check", err or "")])
        return LLMResult(sorted(set(parsed.revealed_ids)), ids)

    def claim_check(self, utterance: str, evidence: list[EvidenceSnippet], seed: Optional[int] = None,
                    ctx: Optional[ConversationContext] = None) -> LLMResult[Optional[list[ClaimItem]]]:
        valid = {e.evidence_id for e in evidence} | {"unsupported"}

        def check(o: ClaimOutput) -> Optional[str]:
            bad = [c.evidence_id for c in o.claims if c.evidence_id not in valid]
            return f"evidence_id values {bad} are neither listed evidence ids nor 'unsupported'" if bad else None

        parsed, ids, err = self._call(
            "claim_check", {"utterance": utterance, "evidence_json": [e.model_dump() for e in evidence]},
            ClaimOutput, check, seed, ctx=ctx)
        if parsed is None:
            return LLMResult(None, ids, [self._flag("claim_check", err or "")])
        return LLMResult(list(parsed.claims), ids)
