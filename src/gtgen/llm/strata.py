"""D060 persona diversity helpers: seeded Latin-hypercube strata, company uniqueness, regeneration loop.

Everything here is pure / deterministic (no LLM except through the runner passed to ``persona_unique``).
"""
from __future__ import annotations

import hashlib
import math
import re
from difflib import SequenceMatcher
from fractions import Fraction
from typing import Any, Mapping, Optional, Sequence

import numpy as np


#: Traits that are integer valued (strata map to integer sub-ranges, shared when n exceeds the integer count).
DISCRETE_TRAITS = ("patience_rounds", "max_relaxations")
CONTINUOUS_TRAITS = ("decision_randomness",)
_LEGAL_TOKENS = {"pvt", "ltd", "private", "limited", "llp", "inc", "co", "company", "corp", "corporation", "and", "the"}


def _perm(run_key: str, trait: str, n: int) -> np.ndarray:
    digest = hashlib.sha256(f"{run_key}|{trait}".encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big")).permutation(n)


def _unit_float(key: str) -> float:
    """A deterministic float in [0, 1) from an arbitrary string key (sha256-based, same technique as ``_perm``)."""
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def tau_band_for_bid(bid_id: str, run_key: str, settings: Mapping,
                     width: Optional[float] = None, jitter: Optional[float] = None,
                     persona_idx: int = 0, n_personas: int = 1) -> list[float]:
    """D080 (user decision, 2026-11-01): tau tied to the BID, not to a run-wide diversity slot - "derive the tau
    from the bid but there can be a randomness each time it is run... mostly the same tau".

    The band is centred on a deterministic hash of ``(bid_id, persona_idx)`` (so the same bid lands in roughly the
    same place on every run), then nudged by a small hash of ``(bid_id, run_key)`` (a repeat run moves it a little
    without ever pinning an exact number). When a bid is given several personas, ``persona_idx`` selects a disjoint
    sub-band of [0, 1] so the personas do not share a tau; with one persona the whole range is available.
    ``width``/``jitter`` default to ``persona.tau_bid.{width,jitter}`` (else 0.08 / 0.05) and are the band's total
    width and the nudge's max magnitude, both in tau units.
    """
    cfg = (settings.get("persona", {}) or {}).get("tau_bid", {}) or {}
    width = float(cfg.get("width", 0.08)) if width is None else width
    jitter = float(cfg.get("jitter", 0.05)) if jitter is None else jitter
    n_personas = max(1, int(n_personas))
    idx = int(persona_idx) % n_personas
    lo_sub, hi_sub = (0.0, 1.0) if n_personas == 1 else (idx / n_personas, (idx + 1) / n_personas)
    anchor = _unit_float(f"tau_anchor|{bid_id}|{idx}")
    centre = lo_sub + (hi_sub - lo_sub) * anchor
    nudge = (_unit_float(f"tau_jitter|{bid_id}|{run_key}") - 0.5) * 2 * min(jitter, (hi_sub - lo_sub) / 4)
    centre = max(lo_sub, min(hi_sub, centre + nudge))
    # Keep the configured width when the sub-band is wide enough: slide the band inside instead of clamping one edge.
    width = min(width, hi_sub - lo_sub)
    lo, hi = centre - width / 2, centre + width / 2
    if lo < lo_sub:
        lo, hi = lo_sub, lo_sub + width
    elif hi > hi_sub:
        lo, hi = hi_sub - width, hi_sub
    if hi <= lo:                       # degenerate width: keep a tiny non-empty band inside the sub-band
        hi = min(hi_sub, lo + 0.01)
        lo = max(lo_sub, hi - 0.01)
    return [round(lo, 3), round(hi, 3)]


def personas_for_category(product_category: Optional[str]) -> int:
    """D080/D089: the number of tau sub-bands a product category uses.

    The five pilot categories keep their tuned counts: cables 2, blankets/pumps 1, transformers/valves 2,
    office/laptop bags 3. Every other category now defaults to 1 instead of 2, so uncatalogued bids do not
    silently get a second persona.
    """
    cat = (product_category or "").lower()
    if "blanket" in cat or "pump" in cat:
        return 1
    if "cable" in cat or "transformer" in cat or "valve" in cat or "sluice" in cat:
        return 2
    if "office bag" in cat or "laptop" in cat:
        return 3
    return 1


def _float_bands(lo: float, hi: float, n: int) -> list[list[float]]:
    lo_f, hi_f = Fraction(str(lo)), Fraction(str(hi))
    w = (hi_f - lo_f) / n
    return [[round(float(lo_f + k * w), 3), round(float(lo_f + (k + 1) * w), 3)] for k in range(n)]


def _int_bands(lo: int, hi: int, n: int) -> list[list[int]]:
    """n strata over the integers lo..hi: disjoint integer sub-ranges, or a shared single integer when n > count."""
    m = hi - lo + 1
    out: list[list[int]] = []
    for k in range(n):
        a, b = lo + Fraction(k * m, n), lo + Fraction((k + 1) * m, n)
        first, last = math.ceil(a), math.ceil(b) - 1
        if first > last:                                   # stratum narrower than one integer: share the nearest one
            first = last = min(hi, max(lo, math.floor((a + b) / 2)))
        out.append([first, min(last, hi)])
    return out


def tau_bands(settings: Mapping, n: int, run_key: str) -> list[list[float]]:
    """n tau strata, ascending. Every stratum lies inside ONE tier (so tier binning is unambiguous) and the n strata
    are spread over the four tiers as evenly as possible (counts differ by at most one; the tiers that get the
    extra stratum are drawn from the seeded permutation), so every tier is covered when n >= 4."""
    bounds = sorted(float(v) for v in settings["persona"]["tiers"].values())
    lows = [0.0] + bounds[:-1]
    highs = [min(b, 1.0) for b in bounds]
    t = len(bounds)
    counts = [n // t] * t
    for tier in _perm(run_key, "tau_tier_extras", t)[: n % t]:
        counts[int(tier)] += 1
    out: list[list[float]] = []
    for k, cnt in enumerate(counts):
        if not cnt:
            continue
        lo = lows[k] + (0.05 if k == 0 else 0.01)          # same padding as the D036 single band
        hi = highs[k] - 0.01
        out.extend(_float_bands(lo, hi, cnt))
    return out


def with_location(categories: Sequence[str], settings: Mapping) -> list[str]:
    """D076: add the own persona category for preferred_states (settings relaxation.location.persona_category,
    default LOCATION) when location relaxation is enabled; sorted, de-duplicated."""
    loc = (settings.get("relaxation", {}) or {}).get("location", {}) or {}
    cats = {c for c in categories if c}
    if loc.get("enabled", False):
        cats.add(str(loc.get("persona_category", "LOCATION")))
    return sorted(cats)


def persona_strata(seed: int, n: int, run_key: str, settings: Mapping, categories: Sequence[str],
                   index: Optional[int] = None) -> dict[str, Any]:
    """Latin-hypercube strata for scenario ``index`` (default ``seed % n``) of a run of ``n`` scenarios.

    Per trait an independent permutation of the n strata is seeded from sha256(run_key, trait), so across the n
    scenarios of one run every stratum of every trait is used exactly once. Returns
    ``{trait: {"index": k, "of": n, "band": [lo, hi]}}`` for tau, patience_rounds, decision_randomness,
    max_relaxations and ``{"tolerance": {category: {...}}}`` (one independent permutation per category). Traits are
    limited to ``persona.diversity.stratified_traits``; ranges come from ``relaxation.persona_ranges``.
    """
    if n < 1:
        raise ValueError("persona_strata: n must be >= 1")
    pos = (seed if index is None else index) % n
    wanted = list(settings["persona"]["diversity"]["stratified_traits"])
    rg = settings["relaxation"]["persona_ranges"]

    def pick(trait: str, bands: list) -> dict:
        k = int(_perm(run_key, trait, n)[pos])
        return {"index": k, "of": n, "band": list(bands[k])}

    out: dict[str, Any] = {}
    if "tau" in wanted:
        out["tau"] = pick("tau", tau_bands(settings, n, run_key))
    for tr in DISCRETE_TRAITS:
        if tr in wanted:
            out[tr] = pick(tr, _int_bands(int(rg[tr][0]), int(rg[tr][1]), n))
    for tr in CONTINUOUS_TRAITS:
        if tr in wanted:
            out[tr] = pick(tr, _float_bands(float(rg[tr][0]), float(rg[tr][1]), n))
    if "tolerance" in wanted:
        bands = _float_bands(float(rg["tolerance"][0]), float(rg["tolerance"][1]), n)
        out["tolerance"] = {c: pick(f"tolerance:{c}", bands) for c in with_location(categories, settings)}
    return out


# ---------------------------------------------------------------- company uniqueness (no LLM)
def _name_tokens(name: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    core = [t for t in toks if t not in _LEGAL_TOKENS]
    return core or toks


def company_similarity(a: str, b: str) -> float:
    """0..1 similarity of two company names after dropping case, punctuation and legal suffixes (Pvt Ltd, ...).
    max(character ratio of the normalised names, token Jaccard, token containment when the shorter has >= 2 tokens)."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return 0.0
    na, nb = " ".join(ta), " ".join(tb)
    sa, sb = set(ta), set(tb)
    score = max(SequenceMatcher(None, na, nb).ratio(), len(sa & sb) / len(sa | sb))
    if min(len(sa), len(sb)) >= 2:
        score = max(score, len(sa & sb) / min(len(sa), len(sb)))
    return round(score, 4)


def _field(p: Any, key: str) -> str:
    v = p.get(key) if isinstance(p, Mapping) else getattr(p, key, "")
    return (v or "").strip()


def near_duplicate_reason(profile: Any, accepted_profiles: Sequence[Any], settings: Mapping,
                          n: Optional[int] = None) -> Optional[str]:
    """Why ``profile`` is not acceptable next to ``accepted_profiles`` (None = unique).

    Name rule: similarity >= persona.diversity.company_name_similarity. Industry/city rule (only when the run size
    ``n`` is given): the (industry, city) pair, counting this profile, may not exceed
    max(1, floor(max_same_industry_city_share * n)) scenarios of the run.
    """
    div = settings["persona"]["diversity"]
    thr = float(div["company_name_similarity"])
    name = _field(profile, "company_name")
    for p in accepted_profiles:
        sim = company_similarity(name, _field(p, "company_name"))
        if sim >= thr:
            return f"company_name {name!r} is too similar ({sim:.2f}) to {_field(p, 'company_name')!r}"
    if n is not None:
        pair = (_field(profile, "industry").lower(), _field(profile, "city").lower())
        same = sum(1 for p in accepted_profiles
                   if (_field(p, "industry").lower(), _field(p, "city").lower()) == pair) + 1
        allowed = max(1, math.floor(float(div["max_same_industry_city_share"]) * n + 1e-9))
        if same > allowed:
            return f"(industry, city) {pair} would appear {same} times in a run of {n} (allowed {allowed})"
    return None


def is_near_duplicate(profile: Any, accepted_profiles: Sequence[Any], settings: Mapping,
                      n: Optional[int] = None) -> bool:
    return near_duplicate_reason(profile, accepted_profiles, settings, n) is not None


def _sales_dict(p: Any) -> dict:
    """speaking_style / sales_approach of a sales persona given as dict or pydantic object.

    D078: no ``name`` field any more (the negotiator never introduces itself by name); a bare string is treated
    as a ready-made ``speaking_style`` (there is no other field to put it in)."""
    if isinstance(p, str):
        return {"speaking_style": p, "sales_approach": ""}
    return {k: _field(p, k) for k in ("speaking_style", "sales_approach")}


def sales_duplicate_reason(sales: Any, accepted_sales: Sequence[Any], threshold: float = 0.8) -> Optional[str]:
    """D078: why ``sales`` (speaking_style / sales_approach) is not distinct from ``accepted_sales`` (None = distinct).
    Near-identical style / approach text (SequenceMatcher ratio >= threshold)."""
    s = _sales_dict(sales)
    for a in accepted_sales:
        d = _sales_dict(a)
        for k in ("speaking_style", "sales_approach"):
            if s[k] and d[k] and SequenceMatcher(None, s[k].lower(), d[k].lower()).ratio() >= threshold:
                return f"sales_persona {k} is nearly identical to another negotiator's"
    return None


def persona_unique(runners: Any, accepted: Sequence[Any], *, n: Optional[int] = None,
                   accepted_sales: Sequence[Any] = (), **persona_kwargs: Any):
    """Call ``runners.persona(**persona_kwargs)`` until the company is unique among ``accepted`` profiles.

    ``persona_kwargs`` are the ``LLMRunners.persona`` arguments (scenario_seed, strata, bands, ctx, ...). Each retry
    passes ``nonce_bump=attempt`` (new nonce => new prompt => new cache key; deterministic given the seed) and
    ``avoid_profiles`` = the accepted profiles (names, industry, city). At most
    persona.diversity.max_company_regenerations regenerations. If still not unique the LAST result is returned with
    qa_flag ``company_not_unique:<reason>`` (never silent). A failed LLM call is returned as is (no regeneration).
    The result's ``extra["regenerations"]`` is the number of bumps used and its llm_call_ids list every call made.
    """
    settings = runners.settings
    max_regen = int(settings["persona"]["diversity"]["max_company_regenerations"])
    avoid = [{"company_name": _field(p, "company_name"), "industry": _field(p, "industry"), "city": _field(p, "city")}
             for p in accepted]
    for a in accepted_sales:                          # D072/D078: sales personas (style/approach) used in the run so far
        d = _sales_dict(a)
        avoid.append({"sales_style": d["speaking_style"], "sales_approach": d["sales_approach"]})
    ids: list[str] = []
    for attempt in range(max_regen + 1):
        res = runners.persona(avoid_profiles=avoid, nonce_bump=attempt, **persona_kwargs)
        ids += [i for i in res.llm_call_ids if i not in ids]
        if res.value is None:
            res.llm_call_ids = ids
            return res
        reason = near_duplicate_reason(res.value[0], accepted, settings, n)
        if reason is None and accepted_sales and (res.extra or {}).get("sales_persona"):
            reason = sales_duplicate_reason(res.extra["sales_persona"], accepted_sales)
        res.extra = {**(res.extra or {}), "regenerations": attempt}
        if reason is None:
            res.llm_call_ids = ids
            return res
    res.llm_call_ids = ids
    res.qa_flags = [*res.qa_flags, f"{'sales_persona_not_unique' if reason.startswith('sales_persona') else 'company_not_unique'}:{reason}"]
    return res


__all__ = ["persona_strata", "tau_bands", "tau_band_for_bid", "company_similarity", "is_near_duplicate",
           "near_duplicate_reason", "persona_unique", "sales_duplicate_reason"]
