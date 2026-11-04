"""GT Negotiator (barrier-enforced, D004/D030) on the D053 relaxation front with the D056 belief loop.

Inputs: ``CatalogueView`` (constructor + step) and ``TranscriptView`` (step) only. The optional ``alignments``
constructor kwarg (D037 R1) is a per-bid attribute-name mapping; its ``bid_id`` is stripped and it never reaches an LLM
call. ``sales_persona`` is the negotiator's OWN persona (generated once per scenario, D054); ``cache_seed`` only
separates cache keys (``ConversationContext``) and never enters a prompt.

Each ``step`` (it must be fed consecutive prefixes of ONE conversation; it keeps its own belief state):
  (a) re-derive state from the transcript: ``negotiator_interpret`` classifies the buyer's reply to its own last
      proposal per item (ACCEPT -> bound fixed at the proposed level; REFUSE -> max 0; COUNTER v -> max at the level of
      v; UNCLEAR -> unchanged, refused after ``negotiation.unclear_limit``); ``negotiator_extract`` re-reads the
      constraints; the ORIGINAL value of every attribute is pinned at first sight so levels never compound.
  (b) recompute need tables and the joint front with ``matching.build_menu`` over the believed-SOFT, catalogue-
      evidenced rules (believed_hardness is never changed; HARD beliefs and doc-proof beliefs are never relaxed);
  (c) choose: RECOMMEND (believed-feasible >= min_shortlist), else the reference menu
      point (the cheapest) as PROPOSE_RELAXATION, else BEST_AVAILABLE (D050: always ends with an offer). A wrap-up
      request from the buyer (``wrap_up.is_wrap_up``) is never answered with a new proposal.
  D055: if the buyer's last message asks for proof of an attribute the catalogue claims, the step is an ACKNOWLEDGE
      that names the document (``negotiator_acknowledge``, catalogue evidence only).
The negotiator learns nothing from the buyer's hidden decisions (D045): only transcript + catalogue.
The deterministic RECOMMEND / BEST_AVAILABLE utterances are built only from catalogue fields.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from gtgen.contracts import (
    Action, AttributeAlignment, BelievedConstraint, BuyerConstraint, CandidateView, CatalogueView, Compliance,
    ComplianceCell, DocClaim, Hardness, NegotiatorStep, NormalisedValue, Proposal, RankingRow, RelaxationBundle,
    RelaxationOption, ScenarioRole, Speaker, TranscriptView,
)
from gtgen.llm import (
    ConversationContext, DocClaimRequest, EvidenceSnippet, MenuItemView, MenuPointView, NegotiatorInputError,
    ProofRequestView, assert_negotiator_safe, listing_text,
)
from gtgen.matching import (
    EvalContext, agrees_with_reference, apply_levels, baseline_levels, belief_role_reasons, build_menu, build_rules,
    config_from_settings, feasible_ids as _feasible_ids, role_aware_gain, synthetic_normalised, tighten,
)
from gtgen.matching.evidence import parse_number_unit
from gtgen.matching.states import ADJACENCY, norm_state
from gtgen.ranking.matrix import build_ranking_matrix

from ._commercial import default_soft_commercial, is_order_quantity_belief
from gtgen.data.claim_cache import ClaimCache, build_dialogue_claims

from ._verified import dedupe_to_confirm, is_unit_only_value, verified_feasible
from ._beliefs import display_value
from ._best import (
    best_available_facts, best_available_ranking, best_available_view, final_option, option_labels,
)
from ._best import cand_label as _cand_label
from ._proof import first_phrase_for, humanise
from ._relax import (
    bound_snapshot, change_fraction, level_floor, state_level_from_text, state_phrase,
)
from ._util import (
    alignment_dict, attr_tokens, guarded_call, is_supplier_stat_belief, name_tokens, norm_attr, per_belief_alignments,
)
from .wrap_up import is_wrap_up

STUB_REPLY = "Let me check what we can offer on these requirements."
STUB_FINAL = "Here are the options I can offer for your request."
STUB_FINAL_EMPTY = "I have no products in our catalogue that fit this request."
STUB_ACK = "Understood. The supplier will provide the {docs} before we proceed."

MAX_EVIDENCE = 8
MAX_SPEC_ITEMS = 12
MAX_SNIPPET_CHARS = 500
MAX_CLARIFY_TOPICS = 2        # D077: kept only for call_plan's compat estimator (_clarify_topics); clarify itself is gone
MAX_TOPIC_LABEL = 34

_QTY_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(units?|pieces?|pcs|nos|no|sets?)\b", re.I)
_BUDGET_RE = re.compile(
    r"(?:budget(?:\s+(?:of|is|around|about|up\s+to|upto|within))?|₹|rs\.?|inr)\s*[:\s]*(?:₹|rs\.?|inr)?\s*"
    r"([\d,]+(?:\.\d+)?)\s*(lakhs?|lacs?|crores?|cr)?\b", re.I)
_MULT = {"lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "crore": 1e7, "crores": 1e7, "cr": 1e7}


# ---------------------------------------------------------------- D049 amendment: catalogue-backed terms
# Deterministic wording -> catalogue field map for the buyer's commercial terms. The negotiator cannot see
# provenance, so a belief is mapped to a catalogue field ONLY from its own wording. Matching is whole-word on the
# lower-cased alphanumeric tokens of the belief's attribute (then, if that finds nothing, of its value); a phrase is a
# run of consecutive tokens. The dict order is the priority when several terms match (most specific first).
CATALOGUE_TERM_WORDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "qty_vs_moq": (("moq",), ("minimum", "order"), ("min", "order")),
    "preferred_states": (("state",), ("states",), ("location",), ("region",), ("based", "in")),
    "budget_inr": (("budget",), ("price",), ("cost",), ("spend",), ("spending",)),
}
# Units a belief may carry for each numeric term (None / no unit is always accepted); any other unit (HP, kg, ...)
# means the belief is about something else ("motor rating" in HP is not a supplier rating) -> not resolved.
CATALOGUE_TERM_UNITS: dict[str, frozenset[str]] = {
    "budget_inr": frozenset({"inr", "rs", "rupee", "rupees", "\u20b9"}),
    "qty_vs_moq": frozenset({"unit", "units", "pcs", "pc", "piece", "pieces", "nos", "no", "set", "sets"}),
}
_QUANTITY_TERMS = ("budget_inr", "qty_vs_moq")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _has_phrase(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return any(tuple(tokens[i:i + n]) == phrase for i in range(len(tokens) - n + 1))


def _belief_unit(b: BelievedConstraint) -> Optional[str]:
    u = b.unit
    if not u:
        p = parse_number_unit(b.value)
        u = p[1] if p else None
    return u.strip().lower().rstrip(".") if u else None


def catalogue_term(belief: BelievedConstraint) -> Optional[str]:
    """The synthetic commercial term (``budget_inr``, ``qty_vs_moq``, ``preferred_states``; D070: supplier rating and
    years are ranking-only, never terms) a belief talks about, from its wording alone (``CATALOGUE_TERM_WORDS``); else ``None``.
    A numeric term is rejected when the belief carries a unit that does not fit it (``CATALOGUE_TERM_UNITS``).
    Pure, no LLM, no catalogue access."""
    unit = _belief_unit(belief)
    for text in (belief.attribute, belief.value):
        toks = _TOKEN_RE.findall((text or "").lower())
        if not toks:
            continue
        for term, phrases in CATALOGUE_TERM_WORDS.items():
            if any(_has_phrase(toks, ph) for ph in phrases):
                if unit is not None and term in CATALOGUE_TERM_UNITS and unit not in CATALOGUE_TERM_UNITS[term]:
                    return None
                return term
    return None


def catalogue_normalised(beliefs: list[BelievedConstraint], candidates: list[CandidateView],
                         quantity: Optional[float], skip: frozenset[str] = frozenset()
                         ) -> dict[tuple[str, str], NormalisedValue]:
    """Per-candidate NormalisedValues for beliefs that ``catalogue_term`` resolves, keyed
    (product_id, belief.attribute) so ``compliance_matrix(normalised=...)`` and the relaxation search use them.

    Encodings: budget = price x quantity (INR); qty_vs_moq = the candidate's moq in units (compared with the stated
    cap in units; the buyer-side slack fraction is NOT used here); rating = rating; tenure = years; states = the
    candidate's state text. Re-uses the matching module's catalogue-field evaluators via a temporary synthetic
    constraint. ``skip`` = attribute names that already have an alignment (the catalogue spec is the evidence there).
    Budget / moq beliefs without a known quantity, and beliefs whose unit does not fit the term, get no entry
    (-> UNKNOWN). Never raises."""
    out: dict[tuple[str, str], NormalisedValue] = {}
    for b in beliefs:
        term = catalogue_term(b)
        if term is None or b.attribute in skip:
            continue
        if term in _QUANTITY_TERMS and (quantity is None or quantity <= 0):
            continue
        tmp = BuyerConstraint(constraint_id=b.belief_id, bid_id="", golden_param=None, hardness=Hardness.SOFT,
                              provenance="synthetic_commercial", attribute=term)
        try:
            res = synthetic_normalised([tmp], candidates, quantity)
        except ValueError:
            continue
        for (pid, _), nv in res.items():
            if term == "qty_vs_moq":      # belief encoding: the candidate's moq in UNITS (synthetic value is a slack)
                moq = next((c.moq for c in candidates if c.product_id == pid), None)
                if moq is None:
                    continue
                nv = nv.model_copy(update={"value": repr(float(moq))})
            out[(pid, b.attribute)] = nv.model_copy(update={"unit": None})
    return out


_GENERIC_CATALOGUE_VALUES = frozenset({
    "yes", "no", "true", "false", "available", "present", "applicable", "provided", "included",
    "customized", "customised", "custom", "standard", "regular", "normal", "default", "na", "n/a",
})


def _catalogue_values(candidates: list[CandidateView], candidate_attribute: str) -> set[str]:
    """Raw values the catalogue actually lists for one candidate attribute (constraint rows + parsed specs)."""
    ca = (candidate_attribute or "").strip().lower()
    if not ca:
        return set()
    out: set[str] = set()
    for c in candidates:
        for r in c.constraints:
            if (r.attribute or "").strip().lower() == ca or (r.spec_name or "").strip().lower() == ca:
                if r.value is not None:
                    out.add(str(r.value))
                for av in (r.allowed_values or []):
                    out.add(str(av))
        for k, v in (c.specifications or {}).items():
            if str(k).strip().lower() == ca and v is not None and not isinstance(v, (dict, list)):
                out.add(str(v))
    return out


def _specific_catalogue_value(value: str) -> bool:
    """A catalogue value worth normalising to: not a bare number, unit, boolean or generic 'as per ...' text."""
    t = (value or "").strip()
    if len(t) < 2:
        return False
    low = t.lower()
    if low in _GENERIC_CATALOGUE_VALUES or low.startswith("as per "):
        return False
    p = parse_number_unit(t)
    return not (p is not None and p[1] is None)


def _normalised_catalogue_value(value: str, candidates: set[str]) -> Optional[str]:
    """D088: if a verbose extractor value contains (or is contained by) a specific catalogue value, return that
    catalogue value.  Word-boundary matching keeps ``PVC`` out of ``HRPVC`` while still matching ``ST2`` and ``HR``.
    Returns None when there is no safe normalisation (the caller keeps the buyer's wording)."""
    bv = (value or "").strip()
    if not bv:
        return None
    low = bv.lower()
    hits: list[tuple[int, str]] = []
    for cv in candidates:
        if not _specific_catalogue_value(cv):
            continue
        cl = cv.strip().lower()
        if cl == low:
            return None
        left = r"(?<![a-z0-9])"
        right = r"(?![a-z0-9])"
        if re.search(left + re.escape(cl) + right, low) or re.search(left + re.escape(low) + right, cl):
            hits.append((len(cl), cv.strip()))
    if not hits:
        return None
    hits.sort(key=lambda x: (-x[0], x[1].lower()))
    return hits[0][1]


def _find_quantity(transcript: TranscriptView) -> Optional[float]:
    """Quantity stated by the buyer (same regex as ``extract_quantity_budget``) or None when absent."""
    for t in transcript.turns:
        if t.speaker != Speaker.BUYER:
            continue
        m = _QTY_RE.search(t.utterance)
        if m:
            v = _to_float(m.group(1))
            if v and v > 0:
                return v
    return None


def _doc_wording(b: BelievedConstraint) -> str:
    v = (b.value or "").strip()
    if not v or v.lower() in {"yes", "true", "required", "1"}:
        return b.attribute.replace("_", " ").strip()
    return v


def negotiator_seed(scenario_id: str) -> int:
    """R7: reproducible across processes (no Python ``hash``)."""
    return int(hashlib.sha256(scenario_id.encode()).hexdigest()[:8], 16) % (2 ** 31)


def _to_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def extract_quantity_budget(transcript: TranscriptView) -> tuple[float, str, Optional[float]]:
    """R6: quantity / unit / budget from buyer utterances (defaults 1.0, 'units', None)."""
    texts = [t.utterance for t in transcript.turns if t.speaker == Speaker.BUYER]
    qty, unit, budget = 1.0, "units", None
    for txt in texts:
        m = _QTY_RE.search(txt)
        if m:
            v = _to_float(m.group(1))
            if v and v > 0:
                qty, unit = v, m.group(2).lower()
                break
    for txt in texts:
        m = _BUDGET_RE.search(txt)
        if m:
            v = _to_float(m.group(1))
            if v and v > 0:
                mult = _MULT.get((m.group(2) or "").lower(), 1.0)
                budget = v * mult
                break
    return qty, unit, budget




@dataclass
class _PendingItem:
    key: str             # belief key (normalised attribute [+ "#n"]): stable across extractions
    ref: str             # belief id at proposal time
    label: str           # the negotiator's own wording (belief.attribute)
    unit: Optional[str]
    from_value: str
    to_value: str
    base: int            # level the rule sat at when proposed
    level: int           # proposed level
    levels: tuple[float, ...]
    rule: Any = None     # D076: the state rule (mode "set") for a supplier-state widening, else None
    state_text: str = ""  # D076: the spoken form of to_value


@dataclass
class _PendingProposal:
    turn_idx: int        # transcript length when proposed = index of the negotiator turn
    items: list[_PendingItem]


_CLAIM = (Compliance.PASS, Compliance.PENDING_VERIFICATION)


class Negotiator:
    def __init__(self, catalogue: CatalogueView, runners: Any, settings: dict,
                 alignments: Optional[list[AttributeAlignment]] = None, *, verification_roles: bool = False,
                 sales_persona: Optional[dict] = None, cache_seed: int = 0, claim_cache: Any = None) -> None:
        if not isinstance(catalogue, CatalogueView):
            raise TypeError("Negotiator only accepts a CatalogueView")
        if alignments is not None and not all(isinstance(a, AttributeAlignment) for a in alignments):
            raise TypeError("alignments must be a list of AttributeAlignment")
        if sales_persona is not None:
            assert_negotiator_safe(sales_persona)
        self._catalogue = catalogue
        self._runners = runners
        self._settings = settings
        self._sales = sales_persona
        self._cache_seed = int(cache_seed)
        self._scenario_id = catalogue.scenario_id
        self._adict: dict[str, AttributeAlignment] = alignment_dict(alignments, strip_bid=True, pool=catalogue)
        neg = settings["negotiation"]
        self._cfg = config_from_settings(settings)
        self._unit = "supplier"                          # menu unit (matches matching.simulate_negotiation)
        # ---- belief state, re-derived from the transcript only
        self._last_proposal: Optional[_PendingProposal] = None
        self._bounds: dict[str, dict[str, int]] = {}     # belief key -> {"fixed": k} | {"max": k}
        self._unclear: dict[str, int] = {}
        self._ids: dict[str, str] = {}                   # belief key -> stable belief id
        self._key_of: dict[str, str] = {}                # belief id -> belief key
        self._orig: dict[str, BelievedConstraint] = {}   # value pinned at first sight (levels never compound)
        self._acked: set[int] = set()                    # buyer turns already acknowledged (D055)
        self._proof_docs: dict[str, list[str]] = {}      # product_id -> documents the buyer asked for
        self._unclear_limit = int(neg["unclear_limit"])
        self._proposed_keys: set[str] = set()            # belief keys that appeared in any own proposal (D072 ledger)
        self._recommended = False
        self._qa_flags: list[str] = []
        self._min_shortlist = int(neg["min_shortlist"])
        self._rel_tol = float(neg["bundle_search"]["numeric_eq_rel_tolerance"])
        self._unverified = float(settings["ranking"]["unverified_text_factor"])
        self.last_cells: list[ComplianceCell] = []
        self.last_analysis: dict[str, Any] = {}          # read-only record of the latest step (GT logging)
        self._roles: dict[str, ScenarioRole] = {}        # belief roles of the latest step (D048)
        sc = settings["scenario"]
        # D048: when True, a HARD belief with too little catalogue evidence becomes verification-only (unscored).
        self._roles_on = bool(verification_roles)
        self._role_reasons: dict[str, str] = {}
        self._demote_zero_pass = bool(self._settings.get("scenario", {}).get("demote_zero_pass", True))
        self._min_coverage = float(sc["negotiated_min_coverage"])
        self._vo_fail_blocks = bool(sc["verification_only_fail_blocks"])
        self._max_listing = int(settings["doc_claims"]["max_listing_chars"])
        self._qty: Optional[float] = None                # quantity found in the transcript (this step)
        self._seed = negotiator_seed(catalogue.scenario_id)
        self._claim_memo: dict[tuple[str, str], DocClaim] = {}     # (product_id, normalised doc wording)
        self._claim_ids: list[str] = []
        # D071: shared (listing, standard) claim cache; a private one still deduplicates inside the conversation
        self._claim_cache = claim_cache if claim_cache is not None else ClaimCache()
        self.claim_cache_stats: dict[str, int] = {"pairs": 0, "hits": 0, "sent": 0, "dedup": 0}
        self._eids: dict[str, str] = {}                  # product_id -> evidence id of the latest ``_evidence`` call

    # ------------------------------------------------------------ loop hooks (read-only)
    def drain_qa_flags(self) -> list[str]:
        out, self._qa_flags = self._qa_flags, []
        return out

    def _ctx(self, turn_idx: int) -> ConversationContext:
        return ConversationContext(self._scenario_id, self._cache_seed, turn_idx)

    # ------------------------------------------------------------ evidence
    def _snippet_text(self, c: CandidateView) -> str:
        parts = [_cand_label(c)]
        if c.city:
            parts.append(f"located in {c.city}")
        specs = c.specifications or {}
        kv = [f"{k}: {v}" for k, v in list(specs.items())[:MAX_SPEC_ITEMS]
              if isinstance(v, (str, int, float)) and str(v).strip()]
        if kv:
            parts.append("; ".join(kv))
        return ". ".join(parts)

    def _evidence(self, catalogue: CatalogueView, first: list[str], extra: Optional[dict[str, str]] = None
                  ) -> list[EvidenceSnippet]:
        """Evidence snippets, the products in ``first`` first (ids E1..); ``extra[pid]`` is appended to that text."""
        by_id = {c.product_id: c for c in catalogue.candidates}
        order = [p for p in dict.fromkeys(first) if p in by_id]
        order += [c.product_id for c in catalogue.candidates if c.product_id not in set(order)]
        out: list[EvidenceSnippet] = []
        self._eids = {}
        for pid in order:
            if len(out) >= MAX_EVIDENCE and not (extra and pid in extra):
                continue
            text = self._snippet_text(by_id[pid])
            if extra and pid in extra:
                text = f"{text}. {extra[pid]}"
            snip = EvidenceSnippet(evidence_id=f"E{len(out) + 1}", text=text[:MAX_SNIPPET_CHARS * 2])
            try:
                assert_negotiator_safe(snip.model_dump())
            except NegotiatorInputError:
                self._qa_flags.append(f"evidence_dropped_unsafe:{pid}")
                continue
            out.append(snip)
            self._eids[pid] = snip.evidence_id
        return out

    def _final_text(self, kind: str, facts: dict, seed: int, ids: list[str], ctx: Any) -> str:
        """D054: RECOMMEND / BEST_AVAILABLE wording comes from the LLM (``negotiator_final``); ``facts`` are built in code.
        Retry exhaustion raises ScenarioSkipped (D066, not caught)."""
        facts = {**facts, "close": True}                 # D072: the final turn ends the conversation: no question, no invitation
        facts = dedupe_to_confirm(facts)
        if not facts["options"]:                         # nothing to word (the runner needs >= 1 option)
            self._qa_flags.append(f"final_no_options:{kind}")
            return STUB_FINAL_EMPTY
        text, rids = guarded_call(self._runners.negotiator_final, "negotiator_final", STUB_FINAL, self._qa_flags,
                                  kind, facts, sales_persona=self._sales, seed=seed, ctx=ctx)
        ids += rids
        return text

    def _recommend_facts(self, ranking: list[RankingRow], feasible: list[str], catalogue: CatalogueView,
                         beliefs: list[BelievedConstraint], cells: list[ComplianceCell]) -> dict:
        by_id = {c.product_id: c for c in catalogue.candidates}
        fs = set(feasible)
        top = [r for r in ranking if r.product_id in fs and r.product_id in by_id][:2]
        options: list[dict] = []
        confirm: list[str] = []
        for r in top:                                  # D048: attribute names only, from the negotiator's own beliefs
            met, unmet, unk = option_labels(r.product_id, beliefs, cells)
            unver = list(r.verification_required) + list(r.pending_verification_docs)
            options.append(final_option(len(options) + 1, by_id[r.product_id], met=met, unmet=unmet,
                                        to_confirm=unk + unver))
            confirm += unk + unver
        return {"options": options, "to_confirm": [a for a in dict.fromkeys(
            humanise(x) for x in confirm) if a], "note": None}

    def _attach_proof_docs(self, ranking: list[RankingRow]) -> list[RankingRow]:
        """D055: documents the buyer asked for are listed per candidate; nothing else in the row changes."""
        if not self._proof_docs:
            return ranking
        out = []
        for r in ranking:
            docs = self._proof_docs.get(r.product_id)
            if docs:
                r = r.model_copy(update={"pending_verification_docs": sorted(set(r.pending_verification_docs) | set(docs))})
            out.append(r)
        return out

    def _best_available_step(self, transcript: TranscriptView, catalogue: CatalogueView,
                             raw: list[BelievedConstraint], current: list[BelievedConstraint],
                             cells: list[ComplianceCell], ectx: EvalContext, ids: list[str], *,
                             wrapped_up: bool) -> NegotiatorStep:
        """D050: always end with an offer. Selection = top-k by match_original of the ORIGINAL beliefs (before agreed
        relaxations); the score columns come from the current beliefs. Text: catalogue fields + own wording only."""
        cands = catalogue.candidates
        raw_cells = ectx.cells(raw, cands)
        roles = dict(self._roles)
        qty, _unit, budget = extract_quantity_budget(transcript)
        ranking = best_available_ranking(raw, current, cands, raw_cells, cells, self._settings,
                                         quantity=qty, budget_inr=budget, roles=roles or None)
        view = best_available_view(ranking, raw, raw_cells, cells, self._settings, roles or None)
        ranking = self._attach_proof_docs(ranking)
        self._recommended = True
        facts = best_available_facts(view, ranking, cands, raw, raw_cells, wrapped_up=wrapped_up)
        utterance = self._final_text("best_available", facts, self._seed, ids, self._ctx(len(transcript.turns)))
        return NegotiatorStep(beliefs=current, action=Action.BEST_AVAILABLE, ranking=ranking,
                              offered_product_ids=[i.product_id for i in view.items],
                              utterance=utterance, llm_call_ids=ids)

    # ------------------------------------------------------------ belief state
    def _drop_supplier_stats(self, raw: list[BelievedConstraint]) -> list[BelievedConstraint]:
        """D070: rating / years of experience are ranking-only. An extractor that still emits such a belief is ignored
        (no cell, no relaxation, never spoken) unless the belief is aligned to a real catalogue attribute (motor rating)."""
        aligned = per_belief_alignments(raw, self._adict)
        out = [b for b in raw if b.attribute in aligned or not is_supplier_stat_belief(b.attribute, b.value, b.unit)]
        if len(out) != len(raw):
            self._qa_flags.append("supplier_stat_belief_ignored")
        return out

    def _normalise_aligned_values(self, beliefs: list[BelievedConstraint],
                                  candidates: list[CandidateView]) -> list[BelievedConstraint]:
        """D088: replace a verbose extractor value with the catalogue's own value when it is unambiguously contained
        in the buyer's wording.  This keeps ``insulation='HR'`` from becoming an UNKNOWN cell when the buyer said
        ``Heat-resistant cables (HR) as per IS 5831 latest``.  Only aligned, non-numeric ``=`` beliefs are touched;
        lists/``in`` values and numbers keep their original form (units are handled by the matching layer)."""
        aligned = per_belief_alignments(beliefs, self._adict)
        cache: dict[str, set[str]] = {}
        out: list[BelievedConstraint] = []
        for b in beliefs:
            a = aligned.get(b.attribute)
            if a is None or b.is_doc_proof or (b.operator or "=").strip().lower() not in ("=", "==", "eq", "equals"):
                out.append(b)
                continue
            value = b.value or ""
            parsed = parse_number_unit(value)
            if parsed is not None and parsed[1] is None:
                out.append(b)
                continue
            ca = a.candidate_attribute
            vals = cache.get(ca)
            if vals is None:
                vals = _catalogue_values(candidates, ca)
                cache[ca] = vals
            nv = _normalised_catalogue_value(value, vals)
            out.append(b.model_copy(update={"value": nv}) if nv is not None and nv != value else b)
        return out

    def _canonical(self, raw: list[BelievedConstraint]) -> list[BelievedConstraint]:
        """Stable belief ids per attribute and ORIGINAL values: the first value seen for an attribute is pinned, so a
        later extraction that reads an agreed (relaxed) value back from the transcript cannot compound a level."""
        out: list[BelievedConstraint] = []
        seen: dict[str, int] = {}
        for b in raw:
            base = norm_attr(b.attribute) or b.belief_id
            n = seen.get(base, 0)
            seen[base] = n + 1
            key = base if n == 0 else f"{base}#{n}"
            bid = self._ids.get(key)
            if bid is None:
                bid = f"B{len(self._ids) + 1}"
                self._ids[key] = bid
                self._key_of[bid] = key
            pinned = self._orig.get(key)
            if pinned is None:
                self._orig[key] = b
            else:
                b = b.model_copy(update={"operator": pinned.operator, "value": pinned.value, "unit": pinned.unit})
            out.append(b.model_copy(update={"belief_id": bid}))
        return out

    def _name_state_beliefs(self, beliefs: list[BelievedConstraint]) -> list[BelievedConstraint]:
        """D076: a belief about the supplier's state (``catalogue_term`` == preferred_states, a plain list of Indian
        states, no catalogue alignment) gets the canonical attribute name ``preferred_states`` so the matching module's
        state rule (set widening) applies to it. The set value comes from the buyer's own words ('Maharashtra, Karnataka')."""
        aligned = per_belief_alignments(beliefs, self._adict)
        out = []
        for b in beliefs:
            if (b.attribute != "preferred_states" and b.attribute not in aligned and not b.is_doc_proof
                    and catalogue_term(b) == "preferred_states" and (b.operator or "in").lower() in
                    ("in", "=", "==", "eq", "equals")):
                parts = [x.strip() for x in re.split(r",|/|;|\band\b|\bor\b", b.value or "") if x.strip()]
                if parts and all(norm_state(x) in ADJACENCY for x in parts):
                    b = b.model_copy(update={"attribute": "preferred_states", "operator": "in",
                                             "value": ", ".join(parts)})
            out.append(b)
        return out

    def _bounds_by_ref(self, beliefs: list[BelievedConstraint]) -> dict[str, dict[str, int]]:
        return {b.belief_id: dict(self._bounds[self._key_of[b.belief_id]]) for b in beliefs
                if self._key_of.get(b.belief_id) in self._bounds}

    def _apply_responses(self, responses: list[tuple[_PendingItem, str, Optional[str]]]) -> None:
        for it, resp, cv in responses:
            if resp == "ACCEPT":
                self._bounds = tighten(self._bounds, it.key, fixed=it.level)
                self._unclear.pop(it.key, None)
            elif resp == "REFUSE":
                self._bounds = tighten(self._bounds, it.key, max_level=0)
                self._unclear.pop(it.key, None)
            elif resp == "COUNTER":
                self._unclear.pop(it.key, None)
                if it.rule is not None:                    # D076: a counter on a state widening names the states it offers
                    k = state_level_from_text(it.rule, cv)
                    if k is None:
                        self._qa_flags.append(f"unusable_counter:{it.key}")
                        self._bounds = tighten(self._bounds, it.key, max_level=0)
                        continue
                else:
                    frac = change_fraction(it.from_value, cv)
                    if frac is None:
                        self._qa_flags.append(f"unusable_counter:{it.key}")
                        self._bounds = tighten(self._bounds, it.key, max_level=0)
                        continue
                    k = level_floor(it.levels, frac)
                if k >= it.level:                          # a counter at or beyond the proposal is an acceptance
                    self._bounds = tighten(self._bounds, it.key, fixed=it.level)
                else:
                    self._bounds = tighten(self._bounds, it.key, max_level=k if k > it.base else 0)
            else:                                          # UNCLEAR
                n = self._unclear.get(it.key, 0) + 1
                if n >= self._unclear_limit:
                    self._bounds = tighten(self._bounds, it.key, max_level=0)
                    n = 0
                self._unclear[it.key] = n

    @staticmethod
    def _interpret_view(i: int, it: _PendingItem) -> dict:
        if it.rule is not None:                           # D076: said in plain words, no numbers; "supplier
            # location" must match the SAME wording the proposal used (MenuItemView), not the internal
            # "preferred_states" matching name that _name_state_beliefs stamps onto it.label
            return {"index": i, "attribute": "supplier location", "kind": "categorical_add",
                    "from_value": it.from_value, "to_value": it.state_text}
        return {"index": i, "attribute": it.label, "kind": "numeric_widen",
                "from_value": display_value(it.from_value, it.unit, "numeric_widen"),
                "to_value": display_value(it.to_value, it.unit, "numeric_widen")}

    def _interpret(self, transcript: TranscriptView, seed: int, ids: list[str], ctx: ConversationContext
                   ) -> list[tuple[_PendingItem, str, Optional[str]]]:
        """Classify the buyer's reply to our pending proposal. Consumes the pending proposal."""
        pend = self._last_proposal
        if (pend is None or not transcript.turns or transcript.turns[-1].speaker != Speaker.BUYER
                or len(transcript.turns) <= pend.turn_idx):
            return []
        self._last_proposal = None
        payload = [self._interpret_view(i, it) for i, it in enumerate(pend.items)]
        res, rids = guarded_call(self._runners.negotiator_interpret, "negotiator_interpret", None,
                                 self._qa_flags, transcript, payload, seed=seed, ctx=ctx)
        ids += rids
        by_idx = {r.index: r for r in (res or [])}
        out = []
        for i, it in enumerate(pend.items):
            r = by_idx.get(i)
            out.append((it, r.response if r else "UNCLEAR", r.counter_value if r else None))
        return out

    # ------------------------------------------------------------ claims / evaluation context
    def _claims_for(self, beliefs: list[BelievedConstraint], cands: list[CandidateView], ctx: ConversationContext
                    ) -> Optional[dict[tuple[str, str], DocClaim]]:
        """D051: claim records for the doc-proof beliefs, memoised per (product_id, doc wording) for the whole
        conversation. A failed / stubbed batch is "no information" (flag set, never raises). Returns None when
        there is nothing to pass (no doc-proof belief, runners without ``doc_claims``, or no real record), which
        leaves the evidence-based claim detection of the matching module in force."""
        fn = getattr(self._runners, "doc_claims", None)
        docs = [b for b in beliefs if b.is_doc_proof]
        if not callable(fn) or not docs:
            return None
        key_of = {b.belief_id: " ".join(_doc_wording(b).lower().split()) for b in docs}
        requests: list[DocClaimRequest] = []
        seen: set[str] = set()
        for b in docs:
            k = key_of[b.belief_id]
            if k in seen:
                continue
            seen.add(k)
            for c in cands:
                if (c.product_id, k) not in self._claim_memo:
                    requests.append(DocClaimRequest(c.product_id, b.belief_id, _doc_wording(b),
                                                    listing_text(c, self._max_listing)))
        if requests:
            known, missing = build_dialogue_claims(self._claim_cache, requests)     # D071: ask only for what is unknown
            st = self.claim_cache_stats
            st["pairs"] += len(requests)
            st["hits"] += len(known)
            st["sent"] += len(missing)
            st["dedup"] += len(requests) - len(known) - len(missing)
            if missing:
                stub = [DocClaim(product_id=r.product_id, constraint_ref=r.constraint_ref, claims=False,
                                 llm_call_id="failed") for r in missing]
                try:
                    res, rids = guarded_call(fn, "doc_claim", stub, self._qa_flags, missing, seed=self._seed, ctx=ctx)
                except NegotiatorInputError:
                    self._qa_flags.append("claim_request_unsafe")
                    res, rids = stub, []
                self._claim_ids += rids
                if not isinstance(res, list) or len(res) != len(missing):
                    res = stub
                self._claim_cache.put_claims(missing, res)      # failed results are not stored (no information)
            for r, dc in zip(requests, self._claim_cache.broadcast(requests)):
                if dc is not None:
                    self._claim_memo[(r.product_id, " ".join(r.doc_name.lower().split()))] = dc
                elif (r.product_id, " ".join(r.doc_name.lower().split())) not in self._claim_memo:
                    self._claim_memo[(r.product_id, " ".join(r.doc_name.lower().split()))] = DocClaim(
                        product_id=r.product_id, constraint_ref=r.constraint_ref, claims=False, llm_call_id="failed")
        out: dict[tuple[str, str], DocClaim] = {}
        for b in docs:
            for c in cands:
                dc = self._claim_memo.get((c.product_id, key_of[b.belief_id]))
                if dc is not None and dc.llm_call_id != "failed":
                    out[(c.product_id, b.belief_id)] = dc.model_copy(update={"constraint_ref": b.belief_id})
        return out or None

    def _context(self, beliefs: list[BelievedConstraint], cands: list[CandidateView], ctx: ConversationContext
                 ) -> EvalContext:
        """The belief-side ``EvalContext`` (catalogue evidence only; roles by D048 from the catalogue coverage)."""
        pba = per_belief_alignments(beliefs, self._adict)
        norm = catalogue_normalised(beliefs, cands, self._qty, skip=frozenset(pba))
        ectx = EvalContext(alignments=pba, normalised=norm or None, roles=None,
                           doc_claims=self._claims_for(beliefs, cands, ctx), quantity=self._qty,
                           verification_only_fail_blocks=self._vo_fail_blocks, bypassed=None,
                           unverified_factor=self._unverified, rel_tol=self._rel_tol)
        self._roles = {}
        self._role_reasons = {}
        if self._roles_on:
            rr = belief_role_reasons(beliefs, ectx.cells(beliefs, cands), len(cands), self._min_coverage,
                                     demote_zero_pass=self._demote_zero_pass)   # D048 / zero_pass
            roles = {r: v[0] for r, v in rr.items()}
            self._role_reasons = {r: v[1] for r, v in rr.items() if v[0] == ScenarioRole.VERIFICATION_ONLY and v[1]}
            self._roles = roles
            ectx = replace(ectx, roles=roles)
        return ectx

    # ------------------------------------------------------------ D077: clarify removed; kept for call_plan estimates
    def _clarify_topics(self, catalogue: CatalogueView, beliefs: list[BelievedConstraint], pba: Any) -> list[str]:
        """Compat for ``gtgen.data.call_plan`` (token estimates) only: D077 removed the negotiator's CLARIFY turn
        entirely, so this never fires live. Returns the worst-case shape the planner used to size for: two topics of
        maximum length."""
        return [("x" * (MAX_TOPIC_LABEL)) + ": exact value"] * MAX_CLARIFY_TOPICS

    # ------------------------------------------------------------ D055 acknowledge
    # ------------------------------------------------------------ D055 acknowledge
    def _acknowledge(self, transcript: TranscriptView, catalogue: CatalogueView, beliefs: list[BelievedConstraint],
                     cells: list[ComplianceCell], seed: int, ids: list[str], ctx: ConversationContext
                     ) -> Optional[NegotiatorStep]:
        """If the buyer's last message (the reply to our proposal) asks for proof of an attribute the catalogue
        claims, answer with ACKNOWLEDGE naming the document. Transcript + catalogue only."""
        last = transcript.turns[-1]
        by_cell = {(c.product_id, c.constraint_ref): c for c in cells}
        by_id = {c.product_id: c for c in catalogue.candidates}
        views: list[ProofRequestView] = []
        extra: dict[str, str] = {}
        plan: list[tuple[str, str]] = []         # (product_id, phrase)
        used: set[str] = set()
        for b in beliefs:
            toks = attr_tokens(humanise(b.attribute)) | (attr_tokens(b.value) if b.is_doc_proof else set())
            phrase = first_phrase_for(last.utterance, toks, attr_tokens)
            if phrase is None or phrase.lower() in used:
                continue
            claimers = [c for c in catalogue.candidates
                        if (cell := by_cell.get((c.product_id, b.belief_id))) is not None and cell.scored
                        and cell.status in _CLAIM]
            low = last.utterance.lower()           # prefer the candidate the buyer's message names
            claim = next((c for c in claimers if any(n and n.lower() in low for n in (c.supplier_name, c.product_name))),
                         claimers[0] if claimers else None)
            if claim is None:
                self._qa_flags.append(f"proof_request_unclaimed:{norm_attr(b.attribute)}")
                continue
            used.add(phrase.lower())
            cell = by_cell[(claim.product_id, b.belief_id)]
            note = f"The listing states {humanise(b.attribute)}"
            note += f": {cell.evidence}" if cell.evidence else "."
            extra[claim.product_id] = (extra.get(claim.product_id, "") + " " + note).strip()
            plan.append((claim.product_id, phrase))
            views.append(ProofRequestView(attribute=humanise(b.attribute), doc_name=phrase, evidence_id=claim.product_id))
        self._acked.add(last.turn_idx)
        if not views:
            return None
        evidence = self._evidence(catalogue, [p for p, _ in plan], extra)
        views = [v.model_copy(update={"evidence_id": self._eids[v.evidence_id]}) for v in views if v.evidence_id in self._eids]
        if not views:
            return None
        stub = (STUB_ACK.format(docs=" and the ".join(v.doc_name for v in views)), [e.evidence_id for e in evidence][:1])
        reply, rids = guarded_call(self._runners.negotiator_acknowledge, "negotiator_acknowledge", stub,
                                   self._qa_flags, transcript, views, evidence, seed=seed, ctx=ctx,
                                   sales_persona=self._sales)
        ids += rids
        for pid, phrase in plan:
            self._proof_docs.setdefault(pid, [])
            if phrase not in self._proof_docs[pid]:
                self._proof_docs[pid].append(phrase)
        return NegotiatorStep(beliefs=beliefs, action=Action.ACKNOWLEDGE, utterance=reply[0], llm_call_ids=ids)

    # ------------------------------------------------------------ protocol
    def step(self, transcript: TranscriptView, catalogue: CatalogueView) -> NegotiatorStep:
        out = self._step(transcript, catalogue)
        if self._claim_ids:          # doc_claim calls made while evaluating this step
            out = out.model_copy(update={"llm_call_ids": list(out.llm_call_ids) + self._claim_ids})
            self._claim_ids = []
        return out

    def _propose(self, transcript: TranscriptView, catalogue: CatalogueView, beliefs: list[BelievedConstraint],
                 current: list[BelievedConstraint], rules: list, base: dict[str, int], ectx: EvalContext, menu, mp,
                 seed: int, ids: list[str], ctx: ConversationContext) -> NegotiatorStep:
        cands = catalogue.candidates
        by_rule = {r.ref: r for r in rules}
        by_belief = {b.belief_id: b for b in beliefs}
        items: list[RelaxationOption] = []
        views: list[MenuItemView] = []
        pend: list[_PendingItem] = []
        running = dict(base)
        nums: list[Any] = []
        for ch in mp.changes:                              # one per rule raised above its level, sorted by ref
            ref = ch["ref"]
            rule, b = by_rule[ref], by_belief[ref]
            nxt = dict(running)
            nxt[ref] = ch["level"]
            g = role_aware_gain(beliefs, cands, ectx, rules=rules, levels_before=running, levels_after=nxt)
            running = nxt
            is_state = rule.mode == "set"
            items.append(RelaxationOption(
                constraint_ref=ref, kind="categorical_extend" if is_state else "numeric_widen",
                from_value=ch["current_value"], to_value=ch["new_value"],
                normalised_cost=min(1.0, ch["level"] * rule.weight / rule.top), candidates_gained=max(0, g.gain_products),
                feasible_after=len(g.after), gained_product_ids=list(g.gained),
                negotiator_estimated_gain=g.gain_products))
            unit = b.unit or (parse_number_unit(b.value)[1] if parse_number_unit(b.value) else None)
            spoken = state_phrase(ch.get("added_states") or (), bool(ch.get("any_state"))) if is_state else ""
            if is_state:        # D076: plain words (the states to add), no numbers, no internal name
                views.append(MenuItemView(attribute="supplier location", kind="categorical_add",
                                          from_value=ch["current_value"], to_value=spoken, note=spoken))
            else:
                views.append(MenuItemView(attribute=humanise(b.attribute), kind="numeric_widen",
                                          from_value=display_value(ch["current_value"], unit, "numeric_widen"),
                                          to_value=display_value(ch["new_value"], unit, "numeric_widen")))
            # D065: no analysis numbers (percent looser, candidate gain, cost) reach the wording prompt
            pend.append(_PendingItem(key=self._key_of[ref], ref=ref, label=b.attribute, unit=unit,
                                     from_value=ch["current_value"], to_value=ch["new_value"], base=base[ref],
                                     level=ch["level"], levels=tuple(float(x) for x in rule.levels),
                                     rule=rule if is_state else None, state_text=spoken))
        bundle = RelaxationBundle(items=items, joint_gain=max(0, mp.gain.gain_products),
                                  feasible_after=len(mp.gain.after), gained_product_ids=list(mp.gain.gained),
                                  total_normalised_cost=min(1.0, mp.cost))
        view = MenuPointView(items=views)                # D065: extra_options (candidate gain) is NOT passed to the LLM
        evidence = self._evidence(catalogue, list(mp.gain.gained))
        reply, rids = guarded_call(self._runners.negotiator_reply, "negotiator_reply", (STUB_REPLY, []),
                                   self._qa_flags, transcript, None, current, evidence, seed=seed, ctx=ctx,
                                   sales_persona=self._sales, menu_point=view, allowed_numbers=nums)
        ids += rids
        self._last_proposal = _PendingProposal(turn_idx=len(transcript.turns), items=pend)
        self._proposed_keys |= {p.key for p in pend}
        return NegotiatorStep(beliefs=current, action=Action.PROPOSE_RELAXATION,
                              proposal=Proposal(proposer=Speaker.NEGOTIATOR, bundle=bundle),
                              utterance=reply[0], llm_call_ids=ids)

    def _step(self, transcript: TranscriptView, catalogue: CatalogueView) -> NegotiatorStep:
        if not isinstance(transcript, TranscriptView):
            raise TypeError("Negotiator.step accepts only a TranscriptView")
        if not isinstance(catalogue, CatalogueView):
            raise TypeError("Negotiator.step accepts only a CatalogueView")
        seed = self._seed = negotiator_seed(catalogue.scenario_id)
        ctx = self._ctx(len(transcript.turns))
        self._qty = _find_quantity(transcript)
        self._claim_ids = []
        self.last_analysis = {}
        ids: list[str] = []
        wrap = bool(transcript.turns) and transcript.turns[-1].speaker == Speaker.BUYER \
            and is_wrap_up(transcript.turns[-1].utterance)
        # the buyer's reply to our last proposal is the turn BEFORE the public wrap-up request
        reply_view = TranscriptView(scenario_id=transcript.scenario_id, turns=transcript.turns[:-1]) if wrap \
            else transcript

        # (a) re-derive the state from the transcript
        responses = self._interpret(reply_view, seed, ids, ctx)
        self._apply_responses(responses)
        raw, eids = guarded_call(self._runners.negotiator_extract, "negotiator_extract", [],
                                 self._qa_flags, transcript, seed=seed, ctx=ctx)
        ids += eids
        raw = self._drop_supplier_stats(list(raw))
        no_unit = [b for b in raw if not is_unit_only_value(b.value)]               # r6: 'Sqmm' stored as a value
        if len(no_unit) != len(raw):
            self._qa_flags.append("unit_only_value_ignored")
        raw = no_unit
        kept = [b for b in raw if not is_order_quantity_belief(b, self._qty)]      # the order quantity is a brief field
        if len(kept) != len(raw):
            self._qa_flags.append("order_quantity_belief_ignored")
        raw, n_soft = default_soft_commercial(kept, transcript)                    # budget / MOQ: SOFT unless explicit
        if n_soft:
            self._qa_flags.append("commercial_belief_hardness_defaulted")
        cands = catalogue.candidates
        raw = self._normalise_aligned_values(raw, cands)
        beliefs = self._name_state_beliefs(self._canonical(raw))
        ectx = self._context(beliefs, cands, ctx)
        rules = build_rules(beliefs, self._cfg, roles=ectx.roles, candidates=cands)
        bounds = self._bounds_by_ref(beliefs)
        base = baseline_levels(rules, bounds)
        current = apply_levels(beliefs, rules, base)
        cells = ectx.cells(current, cands)
        self.last_cells = cells
        feasible = _feasible_ids(current, cands, cells)
        self.last_analysis = {"bounds_before": bound_snapshot(bounds), "verified_before": len(feasible),
                              "front_points": [], "chosen_point": None, "agrees": None}

        # D055: the buyer's reply to our proposal asked for proof of something the catalogue claims
        if responses and not wrap and transcript.turns and transcript.turns[-1].speaker == Speaker.BUYER \
                and transcript.turns[-1].turn_idx not in self._acked:
            ack = self._acknowledge(transcript, catalogue, current, cells, seed, ids, ctx)
            if ack is not None:
                return ack

        # (c) shortlist reached -> recommend, but only candidates that are VERIFIED feasible (see _verified.py)
        verified = verified_feasible(feasible, current, cells, ectx.roles)
        self.last_analysis["verified_feasible"] = len(verified)
        self.last_analysis["belief_role_reasons"] = dict(self._role_reasons)
        if len(feasible) >= self._min_shortlist and len(verified) < self._min_shortlist:
            self._last_proposal = None
            self._qa_flags.append("nothing_verified")
            if not wrap:
                self.last_analysis["best_available_reason"] = "nothing_verified"
            return self._best_available_step(transcript, catalogue, beliefs, current, cells, ectx, ids,
                                             wrapped_up=wrap)
        if len(verified) >= self._min_shortlist:
            feasible = verified
            self._last_proposal = None
            self._recommended = True
            qty, _unit, budget = extract_quantity_budget(transcript)
            ranking = build_ranking_matrix(current, cands, cells, quantity=qty, budget_inr=budget,
                                           weights=self._settings["ranking"]["default_weights"],
                                           buyer_city=None, settings=self._settings)
            facts = self._recommend_facts(ranking, feasible, catalogue, current, cells)
            utterance = self._final_text("recommend", facts, seed, ids, ctx)
            return NegotiatorStep(beliefs=current, action=Action.RECOMMEND, ranking=self._attach_proof_docs(ranking),
                                  utterance=utterance, llm_call_ids=ids)

        # (b) D072 relaxation loop (mandatory below the shortlist): need tables + the joint front (singles and bundles up to
        # max_bundle_size) from the CURRENT bounds, over the believed-SOFT numeric rules. BEST_AVAILABLE only once it is exhausted.
        menu = build_menu(beliefs, cands, ectx, self._cfg, bounds, unit=self._unit, diagnostic_used=False)
        self.last_analysis["front_points"] = [p.to_dict() for p in menu.points]
        in_menu = {ch["ref"] for p in menu.points for ch in p.changes} if not menu.diagnostic else set()
        ledger = self._attempt_ledger(current, rules, in_menu, bounds)
        self.last_analysis["constraint_attempts"] = ledger
        self.last_analysis["loop_exhausted"] = "pending_budget_exhausted" not in {v["status"] for v in ledger.values()} \
            and (wrap or menu.diagnostic or not menu.points)
        if wrap:
            self._last_proposal = None
            return self._best_available_step(transcript, catalogue, beliefs, current, cells, ectx, ids,
                                             wrapped_up=True)
        if menu.diagnostic or not menu.points:
            # nothing authorised is left (a diagnostic is never proposed): every stated constraint is in the ledger as
            # proposed / accepted / refused / not relaxable / no reachable gain, so the loop is exhausted
            self._last_proposal = None
            self.last_analysis["diagnostic"] = bool(menu.diagnostic)
            return self._best_available_step(transcript, catalogue, beliefs, current, cells, ectx, ids,
                                             wrapped_up=False)
        mp = menu.reference()
        self.last_analysis["chosen_point"] = mp.to_dict()
        self.last_analysis["agrees"] = agrees_with_reference(menu, mp.index)
        return self._propose(transcript, catalogue, beliefs, current, rules, base, ectx, menu, mp, seed, ids, ctx)

    def _attempt_ledger(self, current: list[BelievedConstraint], rules: list, in_menu: set[str],
                        bounds: dict[str, dict[str, int]]) -> dict[str, dict[str, str]]:
        """D072: per stated belief, how far the relaxation loop got. Statuses: doc_immutable / hard_not_relaxed /
        verification_only (never relaxed, checked and refused by rule), accepted / refused_by_buyer / countered /
        proposed (offered at least once), non_relaxable (SOFT without a numeric direction, e.g. a set of states),
        no_reachable_gain (a rule exists but no candidate becomes eligible within its levels), pending_budget_exhausted
        (the menu still offers it but the conversation ends first)."""
        refs = {r.ref for r in rules}
        out: dict[str, dict[str, str]] = {}
        for b in current:
            bb = bounds.get(b.belief_id, {})
            if b.is_doc_proof:
                st = "doc_immutable"
            elif self._roles.get(b.belief_id) == ScenarioRole.VERIFICATION_ONLY:
                st = "verification_only"
            elif b.believed_hardness == Hardness.HARD:
                st = "hard_not_relaxed"
            elif "fixed" in bb:
                st = "accepted"
            elif bb.get("max") == 0:
                st = "refused_by_buyer"
            elif "max" in bb:
                st = "countered"
            elif self._key_of.get(b.belief_id) in self._proposed_keys:
                st = "proposed"
            elif b.belief_id not in refs:
                st = "non_relaxable"
            elif b.belief_id in in_menu:
                st = "pending_budget_exhausted"
            else:
                st = "no_reachable_gain"
            out[b.belief_id] = {"attribute": b.attribute, "status": st}
            if st == "verification_only":
                out[b.belief_id]["reason"] = self._role_reasons.get(b.belief_id, "low_coverage")
        return out
