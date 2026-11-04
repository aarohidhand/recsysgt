"""Shared helpers for the dialogue package: attribute-name matching, alignment dicts, LLM fallbacks.

Pure string / dict helpers. Nothing here sees hidden buyer state unless the caller passes it in.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, TypeVar

from gtgen.contracts import AttributeAlignment, BelievedConstraint
from gtgen.llm import forbid_template_in_live
from gtgen.matching import feasible_product_ids, hardness_map

T = TypeVar("T")

_SPLIT = re.compile(r"[^a-z0-9]+")
TOKEN_OVERLAP_MIN = 0.5


def norm_attr(s: Optional[str]) -> str:
    """Normalised attribute name: lower-case, alphanumerics only."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def attr_tokens(s: Optional[str]) -> set[str]:
    return {t for t in _SPLIT.split((s or "").lower()) if t}


_STOP = frozenset({"of", "the", "and", "for", "a", "an", "in", "to", "with", "at", "or"})
_SYN = {"colour": "color", "colours": "color", "colors": "color", "webbing": "webbing", "zippered": "zipper",
        "zippers": "zipper", "zipped": "zipper", "wattage": "power", "qty": "quantity"}

# D088: extractors often use the buyer's short wording ("cable size", "insulation", "sheath") while the
# catalogue/truth uses the canonical field name ("size_sqmm", "insulation_type", "sheath_type").  A plain token
# overlap gives "cable size" and "cable type" the same score, so the tie-break can pick the wrong field.  Canonicalise
# the common cable aliases before the exact-name test; unknown names are unchanged.
_ATTR_ALIASES = {
    "cablesize": "sizesqmm", "cabledimension": "sizesqmm", "cabledimensions": "sizesqmm",
    "cablesizemin": "sizesqmm", "cablesizemax": "sizesqmm", "sizesqmm": "sizesqmm",
    "insulation": "insulationtype", "insulationtype": "insulationtype",
    "sheath": "sheathtype", "sheathtype": "sheathtype",
    "submersiblecablequantity": "totalquantity", "cablequantity": "totalquantity",
    "orderquantity": "totalquantity", "totalquantity": "totalquantity", "quantity": "totalquantity",
    "supplierstate": "preferredstates", "supplierstates": "preferredstates",
    "preferredstate": "preferredstates", "preferredstates": "preferredstates",
    "core": "numberofcores", "cores": "numberofcores", "numberofcore": "numberofcores",
    "numberofcores": "numberofcores", "conductor": "conductormaterial",
    "conductormaterial": "conductormaterial",
    # D088 live 209245: the extractor keeps the bid document's long spec names.  Canonicalise them to the
    # buyer/truth attribute names so they do not become unscored cells or get the wrong catalogue field.
    "standardcablelengthinacoilreel": "standardcablelength",
    "standardcablelengthinacoil": "standardcablelength",
    "nameofthelab": "nameoflab",
    "technicalspecifications": "applicablestandard",
}


def canonical_attr(s: Optional[str]) -> str:
    """Canonical attribute name for alignment: lower-case alphanumerics, then the D088 cable alias table."""
    n = norm_attr(s)
    return _ATTR_ALIASES.get(n, n)


def _stem(t: str) -> str:
    t = _SYN.get(t, t)
    if len(t) > 5 and t.endswith("ies"):
        return t[:-3] + "y"
    for suf in ("ing", "ed", "es", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf) and not t.endswith("ss"):
            return t[: -len(suf)]
    return t


def name_tokens(s: Optional[str]) -> frozenset[str]:
    """Lower-cased alphanumeric tokens without stop words, lightly stemmed (colour/color, zippered/zipper, plurals)."""
    return frozenset(_stem(t) for t in attr_tokens(s) if t not in _STOP)


def overlap_ratio(a: Optional[str], b: Optional[str]) -> float:
    """Dice coefficient of the stemmed name tokens: 2|A n B| / (|A| + |B|). Prefers the most specific attribute
    ("strap material" is closer to "strap tape webbing material" than to "inside material")."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    return 2 * len(ta & tb) / (len(ta) + len(tb))


def value_overlap(a: Optional[str], b: Optional[str]) -> float:
    ta, tb = name_tokens(a), name_tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def pair_score(name: Optional[str], other: Optional[str], value: Optional[str] = None,
               other_value: Optional[str] = None) -> float:
    """Score of a (belief name, catalogue / constraint name) pair: exact normalised equality 3.0; else the Dice
    overlap (>= TOKEN_OVERLAP_MIN to count) plus a 0.05 value tie-break; 0.0 when below the threshold."""
    n = canonical_attr(name)
    if n and n == canonical_attr(other):
        return 3.0
    r = overlap_ratio(name, other)
    if r < TOKEN_OVERLAP_MIN:
        return 0.0
    return r + 0.05 * value_overlap(value, other_value)


# D070: supplier rating / years of experience are ranking-only. A belief (or clarify topic) that talks about them is
# ignored by the negotiator (no cell, no relaxation, no menu item, never spoken), tolerant of extractors that still emit it.
# A name counts only when EVERY token is supplier-statistics vocabulary, so "motor rating" or "warranty years" stay real attributes.
_STAT_CORE = frozenset({"rating", "ratings", "review", "reviews", "star", "stars", "tenure", "experience", "established",
                        "year", "years", "yr", "yrs", "business", "trading", "member", "feedback", "reputation"})
_STAT_FILLER = frozenset({"supplier", "vendor", "seller", "company", "min", "minimum", "max", "maximum", "operation",
                          "since", "history", "track", "record", "customer", "score", "age", "old", "min", "required"})
_STAT_UNITS = frozenset({"star", "stars", "rating", "ratings", "year", "years", "yr", "yrs"})
_GENERIC = frozenset({"requirement", "requirements", "criterion", "criteria", "constraint", "need", "preference",
                      "standard", "other", "item", "term"})


def _stat_tokens(text: Optional[str]) -> bool:
    toks = {t for t in _SPLIT.split((text or "").lower()) if t and not t.isdigit()}
    return bool(toks & _STAT_CORE) and toks <= (_STAT_CORE | _STAT_FILLER | _STOP)


def mentions_supplier_stat(text: Optional[str]) -> bool:
    """True when the whole name is supplier-statistics vocabulary (rating, reviews, stars, years in business, ...)."""
    return _stat_tokens(text)


def is_supplier_stat_belief(attribute: Optional[str], value: Optional[str] = None, unit: Optional[str] = None) -> bool:
    """True when a belief is about the supplier's rating / years of experience. A foreign unit (HP, kg, bar...) means
    it is about something else; a generic attribute ("requirement") is judged by its value wording."""
    u = (unit or "").strip().lower().rstrip(".")
    if u and u not in _STAT_UNITS:
        return False
    if _stat_tokens(attribute):
        return True
    atoks = {t for t in _SPLIT.split((attribute or "").lower()) if t}
    return bool(atoks) and atoks <= _GENERIC and _stat_tokens(value)


_BRAND_RE = re.compile(r"\b(brands?|makes?|manufacturers?|models?|make)\b", re.I)


def best_match(name: Optional[str], keys: Iterable[str], value: Optional[str] = None) -> Optional[str]:
    """Exact normalised match first, else the most specific name by Dice token overlap >= 0.5 (first on ties)."""
    best: Optional[str] = None
    best_r = 0.0
    for k in keys:
        r = pair_score(name, k, value, k)
        if r > best_r:
            best, best_r = k, r
    return best


def _pool_support(cands: Iterable[Any], cand_attr: str) -> int:
    """Number of pool candidates carrying non-empty evidence under ``cand_attr`` (constraint rows with a value /
    range / allowed values, or a parsed specification entry; same lookup as matching.evidence)."""
    a = (cand_attr or "").strip().lower()
    n = 0
    for c in cands:
        hit = False
        for r in getattr(c, "constraints", None) or []:
            if (r.attribute.strip().lower() == a or (r.spec_name or "").strip().lower() == a) and (
                    r.value is not None or r.allowed_values or r.min_value is not None or r.max_value is not None):
                hit = True
                break
        if not hit:
            for k, v in (getattr(c, "specifications", None) or {}).items():
                if str(k).strip().lower() == a and v not in (None, "") and not isinstance(v, (dict, list)):
                    hit = True
                    break
        n += hit
    return n


def alignment_dict(alignments: Optional[Sequence[AttributeAlignment]], strip_bid: bool = True,
                   pool: Any = None) -> dict[str, AttributeAlignment]:
    """``{buyer_attribute: alignment}`` (matching takes ONE alignment per buyer attribute). Deterministic choice:
    highest confidence; tie -> candidate_attribute equal (normalised) to the buyer attribute; tie -> more pool
    candidates with non-empty evidence (``pool`` = CatalogueView or a sequence of CandidateView; optional); tie ->
    smallest candidate_attribute name. bid_id stripped (R1) unless ``strip_bid`` is False."""
    cands = list(getattr(pool, "candidates", pool) or []) if pool is not None else []
    support: dict[str, int] = {}

    def key(a: AttributeAlignment) -> tuple:
        if a.candidate_attribute not in support:
            support[a.candidate_attribute] = _pool_support(cands, a.candidate_attribute) if cands else 0
        exact = norm_attr(a.candidate_attribute) == norm_attr(a.buyer_attribute)
        return (-a.confidence, not exact, -support[a.candidate_attribute], a.candidate_attribute)

    best: dict[str, AttributeAlignment] = {}
    for a in alignments or []:
        cur = best.get(a.buyer_attribute)
        if cur is None or key(a) < key(cur):
            best[a.buyer_attribute] = a
    return {k: (a.model_copy(update={"bid_id": ""}) if strip_bid else a) for k, a in best.items()}


def per_belief_alignments(beliefs: Sequence[BelievedConstraint], adict: Mapping[str, AttributeAlignment]
                          ) -> dict[str, AttributeAlignment]:
    """Map each belief's own attribute wording onto the best alignment key: highest score first, ONE belief per
    alignment key (two beliefs never share a catalogue attribute), stable order on ties."""
    keys = sorted(adict)
    names: list[tuple[str, Optional[str]]] = []
    for b in beliefs:
        if b.attribute not in [n for n, _ in names]:
            names.append((b.attribute, b.value))
    pairs = [(pair_score(n, k, v, k), i, j) for i, (n, v) in enumerate(names) for j, k in enumerate(keys)]
    out: dict[str, AttributeAlignment] = {}
    used: dict[int, str] = {}
    for sc, i, j in sorted((p for p in pairs if p[0] > 0), key=lambda p: (-p[0], p[1], p[2])):
        n = names[i][0]
        if n in out:
            continue
        # D088 live 209245: one buyer range can arrive as two beliefs (cable_size_min / cable_size_max).
        # Range variants of the same canonical attribute must share the same catalogue alignment; unrelated
        # beliefs still keep the one-to-one rule.
        canon = canonical_attr(n)
        if j in used and used[j] != canon:
            continue
        out[n] = adict[keys[j]]
        used[j] = canon
    return out


def is_mock() -> bool:
    return os.environ.get("GTGEN_LLM_MODE") == "mock"


def _client_of(fn: Callable[..., Any]) -> Any:
    """The LLM client behind a bound runner method (``LLMRunners.client``), else None (test doubles)."""
    return getattr(getattr(fn, "__self__", None), "client", None)


def guarded_call(fn: Callable[..., Any], prompt_name: str, stub: T, flags: list[str],
                 *args: Any, **kwargs: Any) -> tuple[T, list[str]]:
    """Run a runner call; returns (value, llm_call_ids). Never fabricates content beyond the fixed stub.

    * mock mode + missing fixture (FileNotFoundError) -> stub, flag ``mock_stub:<prompt>``
    * value None (LLM failed after retries)           -> stub, flag ``llm_failed:<prompt>``
    * LIVE mode (D054): neither case may fall back to the stub: the flag is recorded and ``forbid_template_in_live``
      raises ``LiveModeTemplateError``.
    Runner qa_flags are always forwarded. Transport errors propagate.
    """
    client = _client_of(fn)
    try:
        res = fn(*args, **kwargs)
    except FileNotFoundError:
        if not is_mock():
            raise
        flags.append(f"mock_stub:{prompt_name}")
        forbid_template_in_live(client, f"{prompt_name}: mock stub")
        return stub, []
    flags.extend(res.qa_flags)
    if res.value is None:
        flags.append(f"llm_failed:{prompt_name}")
        forbid_template_in_live(client, f"{prompt_name}: failed call")
        return stub, list(res.llm_call_ids)
    return res.value, list(res.llm_call_ids)


def feasible_ids(constraints: Sequence[Any], candidates: Sequence[Any], cells: Sequence[Any]) -> list[str]:
    """``feasible_product_ids`` with the empty-constraint case made explicit: no constraints -> every
    candidate is feasible (``feasible_product_ids`` sees no cells and would return [])."""
    if not constraints:
        return sorted(c.product_id for c in candidates)
    return feasible_product_ids(cells, hardness_map(constraints))
