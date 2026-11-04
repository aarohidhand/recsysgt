"""Ranking matrix (CLAUDE.md §3). One RankingRow per candidate; deterministic.

Formulas
* rating_bayes = ((C*m + n*r) / (C + n)) / 5   in [0,1]   (C = rating_prior_count, m = rating_prior_mean)
* price_score  = (pmax - p) / (pmax - pmin) over ELIGIBLE rows with a price (1.0 if pmax == pmin,
  0.0 if no price); rows outside the pool are clipped to [0,1].
* moq_fit      = 1 if moq <= quantity (or unknown) else quantity / moq
* tenure_years parsed from member_since "N yrs" (D029; no clock); feature = min(years, cap)/cap, 0 if unparseable.
* spec_match   = (PASS + PENDING) / number of constraints
* p_feasible   = prod p_c : PASS/PENDING -> evidence_confidence (already x0.9 when raw_text unverified;
  PENDING identical to PASS); SOFT UNKNOWN 0.5; SOFT FAIL 0.3; HARD FAIL/UNKNOWN 0.
* utility = sum w_i * feature_i (weights normalised to 1); score = p_feasible * utility
* p_recommend = softmax(score / T) over eligible rows, 0 elsewhere; rank by score desc, product_id.
* eligible = no HARD FAIL and no HARD UNKNOWN (ineligible rows stay in the matrix).
"""
from __future__ import annotations

import math
import re
from typing import Iterable, Mapping, Optional, Sequence

from gtgen.contracts import CandidateView, Compliance, ComplianceCell, Hardness, RankingRow
from gtgen.contracts import ScenarioRole
from gtgen.matching.compliance import cells_by_product, is_inactive
from gtgen.matching.recommend import match_original
from gtgen.matching.evidence import ConstraintT, hardness_of, ref_of

FEATURES = ("spec_match", "price_score", "moq_fit", "rating_bayes", "tenure_years", "location_match",
            "evidence_confidence")
_DEFAULTS = {
    "softmax_temperature": 0.15, "p_soft_unknown": 0.5, "p_soft_fail": 0.3, "unverified_text_factor": 0.9,
    "rating_prior_mean": 3.8, "rating_prior_count": 10,
    "default_weights": {"spec_match": 0.28, "price_score": 0.20, "moq_fit": 0.09, "rating_bayes": 0.08,
                        "tenure_years": 0.12, "location_match": 0.15, "evidence_confidence": 0.08},   # D070
}
DEFAULT_SUPPLIER_PRIORITY = ("location_match", "tenure_years", "rating_bayes")   # D070
_REQUIRED = ("rating_max", "tenure_cap_years")   # D029: no in-code fallback
_TENURE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:yrs?|years?)\s*$", re.IGNORECASE)


def rating_bayes(rating: Optional[float], count: Optional[int], prior_mean: float, prior_count: float,
                 rating_max: float = 5.0) -> float:
    n = count or 0
    r = rating if (rating is not None and n > 0) else 0.0
    return ((prior_count * prior_mean + n * r) / (prior_count + n)) / rating_max


def moq_fit(moq: Optional[float], quantity: float) -> float:
    if moq is None or moq <= 0 or moq <= quantity:
        return 1.0
    return quantity / moq


def tenure_years(member_since: Optional[str]) -> Optional[float]:
    """Parse "N yrs" / "N yr" / "N years" (case-insensitive, integer or decimal) -> N; else None."""
    if not member_since:
        return None
    m = _TENURE_RE.match(member_since)
    return float(m.group(1)) if m else None


def tenure_feature(years: Optional[float], cap_years: float) -> float:
    return 0.0 if years is None else min(years, cap_years) / cap_years


def softmax(scores: Sequence[float], temperature: float) -> list[float]:
    if not scores:
        return []
    z = [s / temperature for s in scores]
    m = max(z)
    e = [math.exp(x - m) for x in z]
    t = sum(e)
    return [x / t for x in e]


def enforce_supplier_priority(weights: Mapping[str, float], priority: Sequence[str] = DEFAULT_SUPPLIER_PRIORITY
                              ) -> dict[str, float]:
    """D070: force w[priority[0]] >= w[priority[1]] >= ... among the priority features PRESENT as keys of ``weights``
    (a zero value counts as present; a sparse weights dict that omits a feature does not use it).  Their values are
    sorted descending and reassigned in priority order, so the total is kept; other features are untouched; the
    result is renormalised to sum 1 (an all-zero input is returned unchanged)."""
    w = {k: float(v) for k, v in weights.items()}
    pri = [k for k in priority if k in w]
    for k, v in zip(pri, sorted((max(0.0, w[k]) for k in pri), reverse=True)):
        w[k] = v
    tot = sum(v for v in w.values() if v > 0)
    return {k: v / tot for k, v in w.items()} if tot > 0 else w


def _priority(settings: Optional[Mapping]) -> Sequence[str]:
    rec = (settings or {}).get("recommendation", {}) or {}
    return rec.get("supplier_priority") or DEFAULT_SUPPLIER_PRIORITY


def _cfg(settings: Optional[Mapping]) -> dict:
    r = dict(_DEFAULTS)
    sec = (settings or {}).get("ranking", settings or {})
    r.update({k: v for k, v in (sec or {}).items() if k in _DEFAULTS or k in _REQUIRED})
    for k in _REQUIRED:
        if r.get(k) is None:
            raise KeyError(f"ranking.{k} missing from settings (config/settings.yaml)")
    return r


def _norm_weights(weights: Optional[Mapping[str, float]], default: Mapping[str, float],
                  priority: Sequence[str] = DEFAULT_SUPPLIER_PRIORITY) -> dict[str, float]:
    w = {k: float(v) for k, v in (weights or {}).items() if k in FEATURES}
    if not any(v > 0 for v in w.values()):
        w = {k: float(v) for k, v in default.items() if k in FEATURES}
    w = {k: v for k, v in enforce_supplier_priority(w, priority).items() if v > 0}     # D070
    tot = sum(w.values())
    return {k: v / tot for k, v in w.items()}


_STATE_ATTRS = {"preferred_states", "preferred_state", "supplier_state", "supplier_states"}


def _norm_state(s: object) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def preferred_states_of(constraints: Iterable[ConstraintT]) -> Optional[tuple[str, ...]]:
    """The buyer's preferred states from the (active) ``preferred_states`` term, else None.  A BuyerConstraint gives
    ``allowed_values`` (or the comma/semicolon separated ``buyer_value``); a BelievedConstraint its ``value``."""
    for c in constraints:
        if is_inactive(c) or c.attribute.strip().lower() not in _STATE_ATTRS:
            continue
        vals = list(getattr(c, "allowed_values", None) or [])
        if not vals:
            raw = getattr(c, "buyer_value", None) if hasattr(c, "buyer_value") else getattr(c, "value", None)
            vals = [v for v in re.split(r"[,;|/]|\band\b", str(raw or "")) if v.strip()]
        out = tuple(_norm_state(v) for v in vals if str(v).strip())
        if out:
            return out
    return None


def location_feature(cand: CandidateView, preferred_states: Optional[Sequence[str]],
                     buyer_city: Optional[str]) -> float:
    """Defect 6: 1.0 if the candidate's state is in the buyer's preferred states; with no such term fall back to
    buyer-city equality.  Binary (the data has no nearby-state notion)."""
    if preferred_states:
        ps = {_norm_state(x) for x in preferred_states}
        return 1.0 if (cand.state and _norm_state(cand.state) in ps) else 0.0
    if buyer_city and cand.city and buyer_city.strip().lower() == cand.city.strip().lower():
        return 1.0
    return 0.0


def build_ranking_matrix(constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView],
                         cells: Iterable[ComplianceCell], *, quantity: float,
                         budget_inr: Optional[float] = None,
                         weights: Optional[Mapping[str, float]] = None,
                         buyer_city: Optional[str] = None,
                         settings: Optional[Mapping] = None,
                         original_constraints: Optional[Sequence[ConstraintT]] = None,
                         original_cells: Optional[Iterable[ComplianceCell]] = None,
                         original_roles: Optional[Mapping[str, ScenarioRole]] = None,
                         preferred_states: Optional[Sequence[str]] = None) -> list[RankingRow]:
    """Rows sorted by rank.  Cells with ``scored=False`` (D048) are skipped: they are not in hard_*/soft_*,
    p_feasible, spec_match or evidence_confidence; their constraint attributes go to ``verification_required``.
    DROPPED / DROP / MERGE constraints are ignored.  If ``original_constraints`` and ``original_cells`` are
    given, ``match_original`` (D050, see matching.recommend) is filled; otherwise it stays None.  ``settings`` may be the full settings dict or its ``ranking`` section.
    ``location_match`` = 1 if the candidate state is in ``preferred_states`` (explicit argument, else the active
    ``preferred_states`` term of ``constraints`` / ``original_constraints``), else buyer-city equality when no such
    term exists.  ``price_vs_budget`` = price * quantity / budget_inr (None if either is missing)."""
    cfg = _cfg(settings)
    if preferred_states is None:
        preferred_states = preferred_states_of(constraints) or preferred_states_of(original_constraints or ())
    w = _norm_weights(weights, cfg["default_weights"], _priority(settings))
    constraints = [c for c in constraints if not is_inactive(c)]
    by = cells_by_product(cells)
    attr_of = {ref_of(c): c.attribute for c in constraints}
    hard = {ref_of(c): hardness_of(c) == Hardness.HARD for c in constraints}

    base = []
    for cand in candidates:
        d = by.get(cand.product_id, {})
        cnt = dict(hp=0, hf=0, hu=0, sp=0, sf=0, su=0)
        pend: set[str] = set()
        p_feas = 1.0
        confs: list[float] = []
        n_pass = 0
        n_c = 0
        unscored: set[str] = set()
        for c in constraints:
            ref = ref_of(c)
            cell = d.get(ref)
            if cell is not None and not cell.scored:
                unscored.add(attr_of[ref])
                continue
            n_c += 1
            st = cell.status if cell else Compliance.UNKNOWN
            h = hard[ref]
            if st in (Compliance.PASS, Compliance.PENDING_VERIFICATION):
                cnt["hp" if h else "sp"] += 1
                n_pass += 1
                pc = 1.0 if cell.evidence_confidence is None else cell.evidence_confidence
                p_feas *= pc
                confs.append(pc)
                if st == Compliance.PENDING_VERIFICATION and cell.pending_doc:
                    pend.add(cell.pending_doc)
            elif st == Compliance.FAIL:
                cnt["hf" if h else "sf"] += 1
                p_feas *= 0.0 if h else cfg["p_soft_fail"]
            else:
                cnt["hu" if h else "su"] += 1
                p_feas *= 0.0 if h else cfg["p_soft_unknown"]
        eligible = cnt["hf"] == 0 and cnt["hu"] == 0
        if not eligible:
            p_feas = 0.0
        base.append((cand, cnt, sorted(pend), n_pass, p_feas, confs, eligible, n_c, sorted(unscored)))

    prices = [b[0].price for b in base if b[6] and b[0].price is not None]
    if not prices:
        prices = [b[0].price for b in base if b[0].price is not None]
    pmin, pmax = (min(prices), max(prices)) if prices else (0.0, 0.0)

    rows: list[RankingRow] = []
    for cand, cnt, pend, n_pass, p_feas, confs, eligible, n_c, unscored in base:
        if cand.price is None:
            ps = 0.0
        elif pmax == pmin:
            ps = 1.0
        else:
            ps = min(1.0, max(0.0, (pmax - cand.price) / (pmax - pmin)))
        tenure = tenure_years(cand.member_since)
        feat = {
            "spec_match": (n_pass / n_c) if n_c else 1.0,
            "price_score": ps,
            "moq_fit": moq_fit(cand.moq, quantity),
            "rating_bayes": rating_bayes(cand.rating, cand.rating_count, cfg["rating_prior_mean"],
                                         cfg["rating_prior_count"], cfg["rating_max"]),
            "tenure_years": tenure_feature(tenure, cfg["tenure_cap_years"]),
            "location_match": location_feature(cand, preferred_states, buyer_city),
            "evidence_confidence": (sum(confs) / len(confs)) if confs else 0.0,
        }
        util = sum(w.get(k, 0.0) * v for k, v in feat.items())
        score = p_feas * util if eligible else 0.0
        pvb = (cand.price * quantity / budget_inr) if (cand.price is not None and budget_inr) else None
        rows.append(RankingRow(
            product_id=cand.product_id, supplier_id=cand.supplier_id,
            hard_pass=cnt["hp"], hard_fail=cnt["hf"], hard_unknown=cnt["hu"],
            soft_pass=cnt["sp"], soft_fail=cnt["sf"], soft_unknown=cnt["su"],
            pending_verification_docs=pend, spec_match=feat["spec_match"], price_score=ps,
            price_vs_budget=pvb, moq_fit=feat["moq_fit"], rating_bayes=feat["rating_bayes"],
            rating_count=cand.rating_count or 0, tenure_years=tenure, location_match=feat["location_match"],
            evidence_confidence=feat["evidence_confidence"], p_feasible=p_feas, utility=util, score=score,
            p_recommend=0.0, rank=0, eligible=eligible, verification_required=unscored))

    elig = [r for r in rows if r.eligible]
    probs = softmax([r.score for r in elig], cfg["softmax_temperature"])
    for r, p in zip(elig, probs):
        r.p_recommend = p
    if original_constraints is not None and original_cells is not None:
        mo = match_original(original_constraints, original_cells, settings, roles=original_roles,
                            product_ids=[r.product_id for r in rows])
        for r in rows:
            r.match_original = mo[r.product_id]
    rows.sort(key=lambda r: (not r.eligible, -round(r.score, 12), r.product_id))
    for i, r in enumerate(rows, start=1):
        r.rank = i
    return rows
