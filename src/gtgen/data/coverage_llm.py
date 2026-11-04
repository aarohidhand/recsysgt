"""How much does the real normalise step add over structured evidence? (plan + report)

``plan``   : stratified candidate sample + one snippet-based normalise request per (candidate, active
             HARD constraint); call estimate under a hard cap. Makes NO live LLM call (run it with
             GTGEN_LLM_MODE=mock; ``prepare_bid`` is cache-backed and a miss fails loudly).
``report`` : reads ``<slug>_requests.json`` + ``<slug>_normalised.json`` (written by the llm-engineer's live
             runner) and writes docs/coverage_llm.md. Only GROUNDED results are evidence (rule 1.8).

Interface file written by the live runner (``<slug>_normalised.json``)::

    {"bid", "results": {"r0001": {"value","unit","confidence","llm_call_id","grounded"}},
     "meta": {"normalise_calls", "http_requests", "qa_flags": []}}
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from typing import Optional, Sequence

import numpy as np
import pandas as pd

STATUSES = ["PASS", "FAIL", "PENDING_VERIFICATION", "UNKNOWN"]
STRATA = ["low", "mid", "high"]
SNIPPET_UNITS = 3
SNIPPET_CAP = 500
MIN_N = {0: 16, 1: 12}          # first bid >= 16, second bid >= 12
STOP = frozenset("the and for with from that this are any not all per has have its out can may must "
                 "should shall will use used using type each one two than min max nos".split())
_TOK = re.compile(r"[a-z0-9]+")
_SENT = re.compile(r"(?<=[.!?;])\s+|\n+")


def slug(bid: str) -> str:
    return bid.replace("/", "_")


# ------------------------------------------------------------------ text + tokens
def tokens(s: object) -> set[str]:
    """Lowercase alphanumeric tokens of length >= 3, minus a small stoplist (underscores split)."""
    if s is None:
        return set()
    return {t for t in _TOK.findall(str(s).lower()) if len(t) >= 3 and t not in STOP}


def _spec_lines(spec: Optional[dict]) -> list[str]:
    if not spec:
        return []
    return [f"{k}: {v}" for k, v in spec.items()]


def text_units(cand) -> list[str]:
    """Free-text units of one candidate: spec entries ('key: value'), description and brochure sentences."""
    units = _spec_lines(cand.specifications)
    for blob in (cand.description, cand.brochure_text):
        if blob:
            units += [u.strip() for u in _SENT.split(str(blob)) if u and u.strip()]
    return units


def text_chars(cand) -> int:
    """len(json of specifications) + len(description) + len(brochure_text); missing = 0."""
    n = len(json.dumps(cand.specifications, ensure_ascii=False)) if cand.specifications else 0
    return n + len(cand.description or "") + len(cand.brochure_text or "")


def constraint_tokens(c) -> set[str]:
    return tokens(c.attribute) | tokens(c.spec_name) | tokens(c.commercial_text)


def select_snippet(units: Sequence[str], ctoks: set[str]) -> Optional[str]:
    """Top-3 units by token overlap (>= 1; ties keep source order), joined, capped at 500 chars."""
    scored = []
    for i, u in enumerate(units):
        ov = len(tokens(u) & ctoks)
        if ov >= 1:
            scored.append((-ov, i, u))
    if not scored:
        return None
    scored.sort()
    return "\n".join(u for _, _, u in scored[:SNIPPET_UNITS])[:SNIPPET_CAP]


# ------------------------------------------------------------------ sampling
def stratum_sizes(n: int) -> list[int]:
    """As even as possible, extra items to low then mid (20 -> 7/7/6)."""
    base, rem = divmod(n, 3)
    return [base + (1 if i < rem else 0) for i in range(3)]


def stratified_sample(chars: dict[str, int], n: int, seed: int) -> list[dict]:
    """Terciles of the pool by text_chars (ties by product_id), seeded draw inside each stratum."""
    order = sorted(chars, key=lambda p: (chars[p], p))
    k = len(order)
    sizes = [k // 3 + (1 if i < k % 3 else 0) for i in range(3)]
    cuts = [0, sizes[0], sizes[0] + sizes[1], k]
    pools = [order[cuts[i]:cuts[i + 1]] for i in range(3)]
    out = []
    for si, (name, pool, size) in enumerate(zip(STRATA, pools, stratum_sizes(n))):
        rng = np.random.default_rng([seed, si])
        take = min(size, len(pool))
        idx = sorted(rng.choice(len(pool), size=take, replace=False).tolist()) if take else []
        out += [{"product_id": pool[i], "stratum": name, "text_chars": chars[pool[i]]} for i in idx]
    return out


# ------------------------------------------------------------------ requests
def build_bid_plan(bid: str, constraints, candidates, alignments: dict, settings: dict,
                   n: int, seed: int, structured_status: Optional[dict] = None) -> dict:
    """Sample + requests + counts for one bid. ``constraints`` = active constraints (all hardness)."""
    from gtgen.matching.compliance import compliance_matrix
    hard = [c for c in constraints if getattr(c.hardness, "value", c.hardness) == "HARD"]
    by_pid = {c.product_id: c for c in candidates}
    chars = {c.product_id: text_chars(c) for c in candidates}
    sample = stratified_sample(chars, n, seed)
    sc = [by_pid[s["product_id"]] for s in sample]
    cells = compliance_matrix(
        hard, sc, alignments, None,
        unverified_factor=settings["ranking"]["unverified_text_factor"],
        rel_tol=settings["negotiation"]["bundle_search"]["numeric_eq_rel_tolerance"])
    status = {(c.product_id, h.constraint_id): cell.status.value
              for (c, h), cell in zip(((c, h) for c in sc for h in hard), cells)}
    reqs, counts = [], dict(sampled=len(sample), hard_constraints=len(hard), pairs=len(sample) * len(hard),
                            structured_evidence=0, no_text_match=0, requests=0, unaligned_requests=0)
    for cand in sc:
        units = text_units(cand)
        for h in hard:
            if status[(cand.product_id, h.constraint_id)] != "UNKNOWN":
                counts["structured_evidence"] += 1
                continue
            snip = select_snippet(units, constraint_tokens(h))
            if snip is None:
                counts["no_text_match"] += 1
                continue
            counts["requests"] += 1
            al = alignments.get(h.attribute)
            if al is None or not al.candidate_attribute:
                counts["unaligned_requests"] += 1
            reqs.append({"rid": f"r{counts['requests']:04d}", "product_id": cand.product_id,
                         "constraint_id": h.constraint_id, "attribute": h.attribute, "text": snip,
                         "target": h.attribute, "canonical_unit": h.unit or ""})
    return {"bid": bid, "seed": seed, "sample": sample, "requests": reqs, "counts": counts}


def calls_for(n_requests: int, batch: int) -> int:
    return math.ceil(n_requests / batch) if n_requests > 0 else 0


def plan_budget(builder, n0: int, n1: int, cap: int, reserve: int, batch: int) -> tuple[list[dict], list[int]]:
    """Trim n (second bid first, floor 12; then first bid, floor 16) until total calls <= cap - reserve.

    ``builder(i, n)`` returns the bid plan dict for bid index i and sample size n. Raises if the budget
    cannot be met at the floors, so the cap is never exceeded.
    """
    limit = cap - reserve
    ns = [n0, n1]
    plans = [builder(0, ns[0]), builder(1, ns[1])]

    def total() -> int:
        return sum(calls_for(p["counts"]["requests"], batch) for p in plans)

    for i in (1, 0):
        while total() > limit and ns[i] > MIN_N[i]:
            ns[i] -= 1
            plans[i] = builder(i, ns[i])
    if total() > limit:
        raise SystemExit(f"estimate {total()} calls > cap-reserve {limit} even at minimum n {ns}; "
                         "refusing to plan (cap would be exceeded)")
    return plans, ns


def estimate_table(plans: list[dict], batch: int) -> pd.DataFrame:
    rows = []
    for p in plans:
        c = p["counts"]
        rows.append({"bid": p["bid"], "sampled": c["sampled"], "requests": c["requests"],
                     "no_text_match": c["no_text_match"], "structured_evidence": c["structured_evidence"],
                     "unaligned_requests": c["unaligned_requests"], "calls": calls_for(c["requests"], batch)})
    df = pd.DataFrame(rows)
    tot = {"bid": "TOTAL", **{k: int(df[k].sum()) for k in df.columns if k != "bid"}}
    return pd.concat([df, pd.DataFrame([tot])], ignore_index=True)


# ------------------------------------------------------------------ report
def _num(s: object) -> Optional[float]:
    m = re.search(r"[-+]?\d[\d,]*\.?\d*", str(s))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def is_grounded(value: Optional[str], snippet: str) -> bool:
    """Numbers compared numerically against the numbers in the snippet; text case-insensitively as substring."""
    if value is None or not str(value).strip():
        return False
    v = str(value).strip()
    rng = re.fullmatch(r"\s*([-+]?\d[\d,]*\.?\d*)\s*\.\.\s*([-+]?\d[\d,]*\.?\d*)\s*[A-Za-z%°/²³\.]*", v)
    if rng:      # range 'a..b': both endpoints must occur in the snippet
        return all(is_grounded(e, snippet) for e in rng.groups())
    n = _num(v)
    if n is not None and re.fullmatch(r"[-+]?[\d,]*\.?\d+\s*[A-Za-z%°/²³\.]*", v):
        return any(abs(float(m.replace(",", "")) - n) <= 1e-9 * max(1.0, abs(n))
                   for m in re.findall(r"\d[\d,]*\.?\d*", snippet) if _safe(m))
    return v.lower() in snippet.lower()


# Flow units are NOT in gtgen.matching.units; kept here only to size the potential (clearly labelled).
_EXTRA_FLOW = {"lps": 1.0, "lpm": 1.0 / 60, "lph": 1.0 / 3600}
_NU = re.compile(r"(\d[\d,]*\.?\d*)\s*([A-Za-z]+)?")


def _factor(u: Optional[str], extra: bool) -> Optional[tuple[str, float]]:
    from gtgen.matching import units as U
    k = U.norm_unit(u)
    if k is None:
        return None
    if k in U._TABLE:
        return U._TABLE[k]
    if extra and k in _EXTRA_FLOW:
        return ("flow", _EXTRA_FLOW[k])
    return None


def classify_ungrounded(value: object, unit: Optional[str], snippet: str, extra: bool = False,
                        rel_tol: float = 1e-3) -> tuple[str, str]:
    """Would an ungrounded value be grounded after a deterministic unit conversion?

    Returns ('a'|'b'|'c', reason). a = some number+unit in the snippet converts (via gtgen.matching.units)
    to the returned unit and matches the returned number within ``rel_tol``; b = wrong value (the snippet
    has a convertible number+unit but none matches, or the snippet has no number at all = word-to-number);
    c = not checkable (range / non-numeric value, no unit, or units outside the table).
    ``extra=True`` additionally knows LPS/LPM/LPH (not part of units.py).
    """
    v = str(value).strip() if value is not None else ""
    if re.search(r"\.\.|\d\s*-\s*\d", v) or not re.fullmatch(r"[-+]?\d[\d,]*\.?\d*", v):
        return "c", "range or non-numeric value"
    if not re.search(r"\d", snippet):
        return "b", "word-to-number: no number in the snippet"
    n = float(v.replace(",", ""))
    tgt = _factor(unit, extra)
    if tgt is None:
        return "c", "returned unit missing or outside the unit table"
    checkable = False
    for m in _NU.finditer(snippet):
        src = _factor(m.group(2), extra)
        if src is None or src[0] != tgt[0]:
            continue
        checkable = True
        exp = float(m.group(1).replace(",", "")) * src[1] / tgt[1]
        if abs(exp - n) <= rel_tol * max(abs(exp), abs(n), 1e-12):
            return "a", f"{m.group(1)} {m.group(2)} -> {exp:g} {unit}"
    if checkable:
        return "b", "wrong value: no convertible number+unit in the snippet gives it"
    return "c", "no snippet number has a unit convertible to the returned unit"


def potential_counts(items: Sequence[dict]) -> dict:
    """Counts of a/b/c over ungrounded items, with units.py only and with flow units added."""
    out = {"base": {"a": 0, "b": 0, "c": 0}, "with_flow": {"a": 0, "b": 0, "c": 0}, "n": len(items)}
    for it in items:
        out["base"][it["cls"]] += 1
        out["with_flow"][it["cls_flow"]] += 1
    return out


def _safe(m: str) -> bool:
    try:
        float(m.replace(",", ""))
        return True
    except ValueError:
        return False


def _md(df: pd.DataFrame, pct: Sequence[str] = ()) -> str:
    if df.empty:
        return "_(none)_\n"
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if c in pct and isinstance(v, (float, int)) and not pd.isna(v):
                cells.append(f"{100 * v:.0f}%")
            elif isinstance(v, float):
                cells.append("" if pd.isna(v) else f"{v:.2f}")
            else:
                cells.append(str(v).replace("|", "/"))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def is_aligned(alignments: dict, attribute: str) -> bool:
    """True if the buyer attribute has a candidate-side alignment (pre-e6bd30a evidence condition)."""
    al = alignments.get(attribute)
    return al is not None and bool(al.candidate_attribute)


def aligned_only(normalised: dict, alignments: dict) -> dict:
    """Pre-fix reading: keep only normalised values keyed (product_id, buyer attribute) with an alignment."""
    return {k: v for k, v in normalised.items() if is_aligned(alignments, k[1])}


def side_by_side(cells: pd.DataFrame, grounded_unaligned: int) -> dict:
    """Mean evidence share per HARD constraint and >=25% counts: BEFORE / AFTER aligned-only / AFTER current."""
    per = cells.groupby("constraint_id").apply(
        lambda g: pd.Series({"b": (g.before != "UNKNOWN").mean(), "al": (g.after_aligned != "UNKNOWN").mean(),
                             "cur": (g.after != "UNKNOWN").mean()}), include_groups=False)
    return {"n_hard": int(len(per)), "mean_before": float(per.b.mean()), "mean_aligned": float(per.al.mean()),
            "mean_current": float(per.cur.mean()), "ge25_before": int((per.b >= 0.25).sum()),
            "ge25_aligned": int((per.al >= 0.25).sum()), "ge25_current": int((per.cur >= 0.25).sum()),
            "grounded_unaligned": int(grounded_unaligned)}


def lift_sentence(sbs: dict) -> str:
    """One plain-language sentence: share of the reported lift that comes from unaligned attributes."""
    parts = []
    for b, d in sbs.items():
        tot = d["mean_current"] - d["mean_before"]
        un = d["mean_current"] - d["mean_aligned"]
        parts.append(f"{b}: {100 * un:.1f} of {100 * tot:.1f} points" +
                     (f" ({100 * un / tot:.0f}%)" if tot > 1e-12 else ""))
    return ("Of the reported lift in mean evidence share, the part that comes from unaligned attributes "
            "(values the pre-fix `extract_evidence` discarded) is, per bid: " + "; ".join(parts) + ".")


def side_by_side_section(analyses: dict) -> list[str]:
    sbs = {b: side_by_side(a["cells"], a["quality"]["grounded_unaligned"]) for b, a in analyses.items()}
    rows = [{"bid": b, "HARD": d["n_hard"], "mean_ev_before": d["mean_before"],
             "mean_ev_after_aligned_only": d["mean_aligned"], "mean_ev_after_current": d["mean_current"],
             "ge25_before": d["ge25_before"], "ge25_after_aligned_only": d["ge25_aligned"],
             "ge25_after_current": d["ge25_current"], "grounded_unaligned": d["grounded_unaligned"]}
            for b, d in sbs.items()]
    return [_md(pd.DataFrame(rows), pct=["mean_ev_before", "mean_ev_after_aligned_only", "mean_ev_after_current"]),
            "`after_current` = all grounded normalised values go to `compliance_matrix` (code since commit e6bd30a: "
            "an unaligned attribute uses the normalised value as its only evidence). `after_aligned_only` = the "
            "pre-fix behaviour, reproduced by passing only values whose buyer attribute has a candidate-side "
            "alignment. `grounded_unaligned` = grounded results of unaligned attributes (the ones the fix made "
            "countable). Both readings are computed from the same grounded results.", "",
            lift_sentence(sbs), ""]


def analyse_bid(req: dict, res: dict, constraints, candidates, alignments: dict, settings: dict) -> dict:
    """Before/after cells for the sampled candidates of one bid, using grounded normalised values only."""
    from gtgen.contracts import NormalisedValue
    from gtgen.matching.compliance import compliance_matrix
    hard = [c for c in constraints if getattr(c.hardness, "value", c.hardness) == "HARD"]
    by_pid = {c.product_id: c for c in candidates}
    sample = req["sample"]
    sc = [by_pid[s["product_id"]] for s in sample]
    stratum = {s["product_id"]: s["stratum"] for s in sample}
    results = res.get("results", {})
    kw = dict(unverified_factor=settings["ranking"]["unverified_text_factor"],
              rel_tol=settings["negotiation"]["bundle_search"]["numeric_eq_rel_tolerance"])
    nv, q = {}, {"requests": len(req["requests"]), "answered": 0, "null": 0, "ungrounded": 0,
                              "grounded": 0, "grounded_unaligned": 0, "missing": 0, "per_constraint": {}, "ungrounded_items": []}
    for r in req["requests"]:
        q["per_constraint"][r["constraint_id"]] = q["per_constraint"].get(r["constraint_id"], 0) + 1
        o = results.get(r["rid"])
        if o is None:
            q["missing"] += 1
            continue
        q["answered"] += 1
        if o.get("value") is None or not str(o["value"]).strip():
            q["null"] += 1
        elif not o.get("grounded") or not is_grounded(o["value"], r["text"]):
            q["ungrounded"] += 1          # runner flag AND our own re-check must both agree
            c0, why0 = classify_ungrounded(o["value"], o.get("unit"), r["text"])
            c1, why1 = classify_ungrounded(o["value"], o.get("unit"), r["text"], extra=True)
            q["ungrounded_items"].append({"rid": r["rid"], "snippet": r["text"], "value": str(o["value"]),
                                          "unit": o.get("unit"), "cls": c0, "why": why0,
                                          "cls_flow": c1, "why_flow": why1})
        else:
            q["grounded"] += 1
            if not is_aligned(alignments, r["attribute"]):
                q["grounded_unaligned"] += 1
            nv[(r["product_id"], r["attribute"])] = NormalisedValue(
                source_text=r["text"], value=str(o["value"]).strip(), unit=o.get("unit"),
                confidence=float(o["confidence"]) if o.get("confidence") is not None else 0.5,
                llm_call_id=o.get("llm_call_id", ""))
    before = compliance_matrix(hard, sc, alignments, None, **kw)
    after = compliance_matrix(hard, sc, alignments, nv, **kw)
    nv_al = aligned_only(nv, alignments)
    after_al = compliance_matrix(hard, sc, alignments, nv_al, **kw)
    pairs = [(c.product_id, h.constraint_id) for c in sc for h in hard]
    df = pd.DataFrame({"product_id": [p for p, _ in pairs], "constraint_id": [c for _, c in pairs],
                       "before": [c.status.value for c in before], "after": [c.status.value for c in after],
                       "after_aligned": [c.status.value for c in after_al]})
    df["stratum"] = df["product_id"].map(stratum)
    attr = {h.constraint_id: h.attribute for h in hard}
    return {"cells": df, "quality": q, "attr": attr, "meta": res.get("meta", {}), "n_sample": len(sc)}


def _share_row(g: pd.DataFrame) -> dict:
    n = len(g)
    r = {"n": n, "ev_before": (g.before != "UNKNOWN").mean() if n else float("nan"),
         "ev_after": (g.after != "UNKNOWN").mean() if n else float("nan")}
    for s in STATUSES:
        r[s] = (g.after == s).mean() if n else float("nan")
    return r


def headline(cells: pd.DataFrame) -> dict:
    per = cells.groupby("constraint_id").apply(
        lambda g: pd.Series({"b": (g.before != "UNKNOWN").mean(), "a": (g.after != "UNKNOWN").mean()}),
        include_groups=False)

    def clean(col: str) -> float:
        bad = cells.assign(x=cells[col].isin(["FAIL", "UNKNOWN"])).groupby("product_id")["x"].any()
        return float((~bad).mean()) if len(bad) else float("nan")
    return {"mean_before": float(per.b.mean()), "mean_after": float(per.a.mean()),
            "ge25_before": int((per.b >= 0.25).sum()), "ge25_after": int((per.a >= 0.25).sum()),
            "n_hard": int(len(per)), "clean_before": clean("before"), "clean_after": clean("after")}


def interpretation(plans: dict, analyses: dict) -> list[str]:
    """Decision-maker paragraph (<= 10 lines); every number is computed from the analyses."""
    hs = {b: headline(a["cells"]) for b, a in analyses.items()}
    qs = {b: a["quality"] for b, a in analyses.items()}
    nreq = sum(q["requests"] for q in qs.values())
    nnull = sum(q["null"] for q in qs.values())
    ngr = sum(q["grounded"] for q in qs.values())
    nun = sum(q["ungrounded"] for q in qs.values())
    parts = []
    for b, h in hs.items():
        parts.append(f"{b}: mean evidence share per HARD constraint {100 * h['mean_before']:.1f}% -> "
                     f"{100 * h['mean_after']:.1f}%, HARD constraints with >=25% evidence {h['ge25_before']} -> "
                     f"{h['ge25_after']} of {h['n_hard']}, candidates with zero HARD FAIL and zero HARD UNKNOWN "
                     f"{100 * h['clean_before']:.0f}% -> {100 * h['clean_after']:.0f}%")
    strat = []
    for b, a in analyses.items():
        c = a["cells"]
        lo, hi = c[c.stratum == "low"], c[c.stratum == "high"]
        strat.append(f"{b} low {100 * (lo.after != 'UNKNOWN').mean():.0f}% / mid "
                     f"{100 * (c[c.stratum == 'mid'].after != 'UNKNOWN').mean():.0f}% / high "
                     f"{100 * (hi.after != 'UNKNOWN').mean():.0f}%")
    lift = [h["mean_after"] - h["mean_before"] for h in hs.values()]
    clean_any = any(h["clean_after"] > 0 for h in hs.values())
    small = max(h["mean_after"] for h in hs.values()) < 0.25
    L = ["**Interpretation.** " + "Sample: " + ", ".join(
        f"{a['n_sample']} candidates for {b}" for b, a in analyses.items())
         + " (stratified by free-text volume); snippets are selected lexically, so every figure below is a LOWER "
           "bound on what a better text retrieval could recover.",
         "- " + "; ".join(parts) + ".",
         f"- Does more free text help? Evidence share AFTER by text stratum: " + "; ".join(strat)
         + " (see section 3; single-bid, small-n differences).",
         f"- {nnull} of {nreq} normalise requests ({100 * nnull / max(nreq, 1):.0f}%) returned null; {ngr} were "
         f"grounded and used, {nun} were ungrounded and discarded.",
         f"- Lift in mean evidence share: " + ", ".join(f"{100 * x:+.1f} pts" for x in lift)
         + (". The lift is small." if small else ".")
         + (" Some sampled candidates have no HARD FAIL/UNKNOWN." if clean_any else
            " No sampled candidate reaches zero HARD FAIL and zero HARD UNKNOWN."),
         "- " + lift_sentence({b: side_by_side(a["cells"], a["quality"]["grounded_unaligned"])
                               for b, a in analyses.items()}),
         "- Conclusion: " + ("the conclusion of docs/coverage.md stands: catalogue evidence is too sparse for "
                             "HARD-constraint feasibility; the real normalise step does not rescue it."
                             if small and not clean_any else
                             "the conclusion of docs/coverage.md is weakened and needs re-checking.")]
    return L + [""]


def potential_section(analyses: dict) -> list[str]:
    L = ["## 7. Potential if units were converted in code (sizing only)", "",
         "Not part of any number above. For each UNGROUNDED normalise result: does the snippet contain a "
         "number+unit that `gtgen.matching.units` converts to the returned unit and matches the returned number "
         "within 1e-3 relative? (a) = correct conversion the code could have done itself; (b) = wrong value "
         "(arithmetic error, wrong dimension, or word-to-number); (c) = not checkable (range, no/unknown unit). "
         "A second column re-classifies with flow units LPS/LPM/LPH added (these are NOT in units.py; "
         "needs a units.py change). Ungrounded values are never used as evidence; this sizes the gain of a "
         "prompt design where the model returns the value in the snippet's own unit and code converts.", ""]
    rows, det = [], []
    for b, a in analyses.items():
        items = a["quality"]["ungrounded_items"]
        pc = potential_counts(items)
        rows.append({"bid": b, "ungrounded": pc["n"], "a_units_py": pc["base"]["a"], "b_units_py": pc["base"]["b"],
                     "c_units_py": pc["base"]["c"], "a_with_flow": pc["with_flow"]["a"],
                     "b_with_flow": pc["with_flow"]["b"], "c_with_flow": pc["with_flow"]["c"]})
        for it in items:
            det.append({"bid": b, "rid": it["rid"], "snippet": it["snippet"].replace("\n", " / ")[:90],
                        "returned": f"{it['value']} {it['unit'] or ''}".strip(), "units_py": it["cls"],
                        "with_flow": it["cls_flow"], "reason": it["why_flow"]})
    L += [_md(pd.DataFrame(rows)), "Per item:", "", _md(pd.DataFrame(det))]
    return L


def render_report(plans: dict[str, dict], analyses: dict[str, dict], date: str = "",
                  live_total_note: str = "") -> str:
    L = ["# Coverage with the real align + normalise pipeline (sampled)", "",
         f"_Generated by `python -m gtgen.data.coverage_llm report`{(' on ' + date) if date else ''}; "
         "all numbers are computed from the run artifacts._", "",
         *interpretation(plans, analyses),
         "## 1. Method, sample, calls", "",
         "- Per bid: `prepare_bid` (reframe, doc_classify, align; cache-backed) gives the ACTIVE constraints, "
         "the catalogue and the alignments.",
         "- Sample: candidates of the bid pool split into terciles of `text_chars` = len(json of "
         "specifications) + len(description) + len(brochure_text) (missing = 0): low / mid / high; seeded draw "
         "inside each stratum (as even as possible).",
         "- Requests: for every sampled candidate and every active HARD constraint, one `normalise` request whose "
         "text is up to 3 of the candidate's own free-text units (spec entries, description / brochure sentences) "
         "with the highest token overlap with the constraint's attribute, spec_name and commercial_text, capped at "
         "500 chars. No overlapping unit = no request (evidence none by construction). Pairs that already have "
         "a non-UNKNOWN structured status get no request.",
         "- BEFORE = structured evidence only (`compliance_matrix`, no normalised). AFTER = structured + GROUNDED "
         "normalised values (value occurs in the snippet; ungrounded results are reported, never used).",
         "- Evidence share = share of sampled candidates whose status is not UNKNOWN.", ""]
    rows = []
    for b, p in plans.items():
        c, m = p["counts"], analyses[b]["meta"]
        rows.append({"bid": b, "sampled": c["sampled"], "HARD": c["hard_constraints"], "pairs": c["pairs"],
                     "structured_evidence": c["structured_evidence"], "no_text_match": c["no_text_match"],
                     "requests": c["requests"], "unaligned_requests": c["unaligned_requests"],
                     "normalise_calls": m.get("normalise_calls", "?"), "http_requests (last run only)": m.get("http_requests", "?")})
    L += [_md(pd.DataFrame(rows)),
          "`unaligned_requests` = requests whose constraint has no candidate-side alignment. `http_requests (last "
          "run only)` is the metadata of the most recent runner invocation; a cached re-run overwrites it, so it is "
          "NOT the live total."]
    if live_total_note:
        L += ["", f"**True live total (not derived from the JSON files):** {live_total_note}"]
    L += ["", "AFTER status of unaligned attributes: since commit e6bd30a `extract_evidence` uses a normalised "
          "value even when the buyer attribute has no alignment, so `unaligned_requests` CAN change the AFTER "
          "status. The AFTER columns of this report use the current code (verified below: AFTER current is "
          "computed with all grounded values; AFTER aligned-only reproduces the pre-fix behaviour).", "",
          *side_by_side_section(analyses)]
    flags = [f for a in analyses.values() for f in a["meta"].get("qa_flags", [])]
    L += [f"Runner qa_flags: {len(flags)}" + (": " + "; ".join(map(str, flags[:10])) if flags else ""), ""]

    strat = []
    for b, p in plans.items():
        s = pd.DataFrame(p["sample"])
        g = s.groupby("stratum")["text_chars"].agg(["count", "min", "median", "max"]).reindex(STRATA).reset_index()
        g.insert(0, "bid", b)
        strat.append(g)
    L += ["Sample strata (text_chars):", "", _md(pd.concat(strat, ignore_index=True))]

    L += ["## 2. Per HARD constraint: evidence before / after and status shares after", ""]
    for b, a in analyses.items():
        rows = []
        for cid, g in a["cells"].groupby("constraint_id", sort=False):
            rows.append({"attribute": a["attr"][cid], "requests": a["quality"]["per_constraint"].get(cid, 0),
                         **_share_row(g)})
        L += [f"### {b}", "", _md(pd.DataFrame(rows).drop(columns="n"),
                                  pct=["ev_before", "ev_after", *STATUSES])]

    L += ["## 3. By text-richness stratum (all HARD constraints pooled)", ""]
    for b, a in analyses.items():
        rows = [{"stratum": s, **_share_row(a["cells"][a["cells"].stratum == s])} for s in STRATA]
        L += [f"### {b}", "", _md(pd.DataFrame(rows), pct=["ev_before", "ev_after", *STATUSES])]

    L += ["## 4. Normalise quality", ""]
    rows = []
    for b, a in analyses.items():
        q = a["quality"]
        n = max(q["requests"], 1)
        rows.append({"bid": b, "requests": q["requests"], "answered": q["answered"], "missing": q["missing"],
                     "null_rate": q["null"] / n, "ungrounded_rate": q["ungrounded"] / n,
                     "grounded_rate": q["grounded"] / n,
                     "requests_per_constraint": q["requests"] / max(len(q["per_constraint"]), 1)})
    L += [_md(pd.DataFrame(rows), pct=["null_rate", "ungrounded_rate", "grounded_rate"])]

    L += ["## 5. Headline", ""]
    rows = []
    for b, a in analyses.items():
        h = headline(a["cells"])
        rows.append({"bid": b, "HARD": h["n_hard"], "mean_ev_before": h["mean_before"],
                     "mean_ev_after": h["mean_after"], "HARD_ge25_before": h["ge25_before"],
                     "HARD_ge25_after": h["ge25_after"], "no_HARD_FAIL_or_UNKNOWN_before": h["clean_before"],
                     "no_HARD_FAIL_or_UNKNOWN_after": h["clean_after"]})
    L += [_md(pd.DataFrame(rows), pct=["mean_ev_before", "mean_ev_after", "no_HARD_FAIL_or_UNKNOWN_before",
                                       "no_HARD_FAIL_or_UNKNOWN_after"]),
          "The last two columns are the share of SAMPLED candidates with zero HARD FAIL and zero HARD UNKNOWN "
          "(usually 0: a single uncovered HARD constraint blocks a candidate).", ""]
    L += ["## 6. Caveats", "",
          "- Snippet selection is lexical: a constraint whose words never appear in the candidate's text gets no "
          "request, so the AFTER share is a lower bound for what a semantic retriever could find.",
          "- Sample sizes are small (about 12-20 candidates per bid, 3 strata); shares carry wide uncertainty "
          "(one candidate = 5-8 percentage points). Two bids only.",
          "- Only grounded values count; a correct but re-phrased value (e.g. 'one HP' for '1 HP') is classed "
          "ungrounded.",
          "- Results hold for the sampled bids and strata. Extrapolating to the full pool or other bids is a "
          "rough estimate at best; strata are terciles of THIS pool's text volume.",
          "- Requests are made only for pairs without structured evidence, so AFTER >= BEFORE by construction "
          "(normalised values never overwrite structured rows in this setup; for unaligned attributes they are "
          "the only evidence).", ""]
    L += potential_section(analyses)
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ drivers
LEGACY_NORMALISE_BATCH = 25   # pinned (was settings llm.batch_max_constraints before D064)


def _prepare(bid: str, settings: dict):
    from gtgen.cli import prepare_bid
    from gtgen.llm import LLMClient, LLMRunners
    runners = LLMRunners(LLMClient(settings), settings)
    prep = prepare_bid(bid, settings, runners)
    al = {a.buyer_attribute: a for a in prep.alignments}
    return prep, al


def cmd_plan(args) -> None:
    if os.environ.get("GTGEN_LLM_MODE", "").lower() != "mock":
        raise SystemExit("plan must run with GTGEN_LLM_MODE=mock (no live calls at planning stage)")
    from gtgen.data.loaders import load_settings
    settings = load_settings()
    # D064: batch_max_constraints is now 80 (token-aware batching).  This older coverage script prices normalise calls with a
    # FIXED batch size; pin the legacy value 25 explicitly so its estimates stay comparable with earlier coverage reports.
    batch = LEGACY_NORMALISE_BATCH
    bids = args.bids.split(",")
    ns = [int(x) for x in args.n.split(",")]
    if len(bids) != 2 or len(ns) != 2:
        raise SystemExit("plan expects exactly two bids and two sizes")
    prepared = [_prepare(b, settings) for b in bids]

    def builder(i: int, n: int) -> dict:
        prep, al = prepared[i]
        return build_bid_plan(bids[i], prep.active_constraints, prep.catalogue.candidates, al, settings, n,
                              args.seed)

    plans, final_n = plan_budget(builder, ns[0], ns[1], args.cap, args.reserve, batch)
    os.makedirs(args.out, exist_ok=True)
    for p in plans:
        with open(os.path.join(args.out, f"{slug(p['bid'])}_requests.json"), "w", encoding="utf-8") as f:
            json.dump(p, f, indent=1, ensure_ascii=False)
    tbl = estimate_table(plans, batch)
    total = int(tbl.iloc[-1]["calls"])
    plan = {"bids": bids, "n_requested": ns, "n_final": final_n, "seed": args.seed, "cap": args.cap,
            "reserve": args.reserve, "batch": batch, "estimate_calls": total,
            "estimate_calls_per_bid": {p["bid"]: calls_for(p["counts"]["requests"], batch) for p in plans},
            "align_calls": 0,
            "worst_case_http_if_every_call_retries_twice": 3 * total,
            "within_budget": total <= args.cap - args.reserve,
            "table": json.loads(tbl.to_json(orient="records"))}
    with open(os.path.join(args.out, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=1)
    print("## Estimate (normalise calls; batch = %d; align calls = 0, alignment served from warm cache)" % batch)
    print()
    print(_md(tbl))
    print(f"n requested {ns} -> final n {final_n}; seed {args.seed}")
    print(f"Estimated live calls: {total} (cap {args.cap}, reserve {args.reserve}, budget for calls "
          f"{args.cap - args.reserve}).")
    print(f"Worst case if every call needed the 2 allowed validation retries: {3 * total} HTTP requests.")
    print("Live requests made by this command: 0")


def cmd_report(args) -> None:
    import datetime
    from gtgen.data.loaders import load_settings
    settings = load_settings()
    with open(os.path.join(args.inp, "plan.json"), encoding="utf-8") as f:
        plan = json.load(f)
    plans, analyses = {}, {}
    for b in plan["bids"]:
        with open(os.path.join(args.inp, f"{slug(b)}_requests.json"), encoding="utf-8") as f:
            req = json.load(f)
        with open(os.path.join(args.inp, f"{slug(b)}_normalised.json"), encoding="utf-8") as f:
            res = json.load(f)
        prep, al = _prepare(b, settings)
        plans[b] = req
        analyses[b] = analyse_bid(req, res, prep.active_constraints, prep.catalogue.candidates, al, settings)
    text = render_report(plans, analyses, datetime.date.today().isoformat(), args.live_total_note)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {args.out}")


def main(argv: Optional[Sequence[str]] = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--bids", default="GEM/2019/B/242710,GEM/2020/B/488257")
    p.add_argument("--n", default="20,20")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--cap", type=int, default=60)
    p.add_argument("--reserve", type=int, default=8)
    p.add_argument("--out", default="out/coverage_llm")
    r = sub.add_parser("report")
    r.add_argument("--in", dest="inp", default="out/coverage_llm")
    r.add_argument("--out", default="docs/coverage_llm.md")
    r.add_argument("--live-total-note", default="",
                   help="free text with the true live HTTP total and how it was obtained (rendered verbatim)")
    args = ap.parse_args(argv)
    from gtgen import cli  # noqa: F401  (loads .env; never printed)
    {"plan": cmd_plan, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
