"""Coverage diagnostics: why are HARD constraints UNKNOWN, and do dataset flags explain it?

Pure functions over two DataFrames plus a driver (``python -m gtgen.data.coverage``) that runs
``prepare_bid`` (LLM calls are cache-backed; live requests are counted and capped) and writes
``docs/coverage.md``.

Frames
------
cell frame  : product_id, constraint_id, status, aligned, evidence
              (status in PASS / FAIL / UNKNOWN / PENDING_VERIFICATION; aligned = the candidate has
              any aligned candidate-side evidence for the constraint)
constraint frame : constraint_id, attribute, hardness (HARD|SOFT), constraint_category, matchability,
              indiamart_matchable, administrative_only, is_doc_proof

What-if variants (A-D) are report-layer strings only; ``NOT_ASSESSABLE`` and ``NON_BLOCKING_UNKNOWN``
do not exist in contracts.py and are never written anywhere but the report.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import sys
from typing import Iterable, Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

STATUSES = ["PASS", "FAIL", "UNKNOWN", "PENDING_VERIFICATION"]
NA = "NOT_ASSESSABLE"
NBU = "NON_BLOCKING_UNKNOWN"
VARIANTS = ["A", "B", "C", "D"]
VARIANT_LABEL = {
    "A": "A current rules",
    "B": "B user rule 2",
    "C": "C = B + coverage additions",
    "D": "D = C + all HARD UNKNOWN non-blocking (UPPER BOUND, not a proposal)",
}
BIDS = ["GEM/2019/B/209245", "GEM/2019/B/231682", "GEM/2019/B/242710",
        "GEM/2019/B/341930", "GEM/2020/B/488257", "GEM/2020/B/626099"]
FLAG_COLS = ["indiamart_matchable", "matchability", "administrative_only", "is_doc_proof"]


# ------------------------------------------------------------------ small helpers
def is_true(x: object) -> bool:
    """NaN-safe truthiness of a flag cell (bool, 'True'/'true', pandas NA)."""
    if x is None or x is pd.NA:
        return False
    if isinstance(x, str):
        return x.strip().lower() == "true"
    try:
        if pd.isna(x):
            return False
    except (TypeError, ValueError):
        pass
    return bool(x)


def _flag_str(x: object) -> str:
    if x is None or x is pd.NA:
        return "NA"
    try:
        if pd.isna(x):
            return "NA"
    except (TypeError, ValueError):
        pass
    return str(x)


# ------------------------------------------------------------------ frame builders
def build_cell_frame(constraints, candidates, alignments: dict, *, unverified_factor: float,
                     rel_tol: float) -> pd.DataFrame:
    """Cells WITHOUT the LLM normalise step, plus an ``aligned`` flag per (candidate, constraint)."""
    from gtgen.matching.compliance import check_view
    from gtgen.matching.evidence import as_view

    rows = []
    for cand in candidates:
        for c in constraints:
            v = as_view(c)
            cell, ev = check_view(v, cand, alignments.get(v.attribute), None, unverified_factor, rel_tol)
            rows.append({"product_id": cand.product_id, "constraint_id": v.ref,
                         "status": cell.status.value, "aligned": bool(ev.usable or ev.claim),
                         "evidence": cell.evidence})
    return pd.DataFrame(rows, columns=["product_id", "constraint_id", "status", "aligned", "evidence"])


def build_constraint_frame(constraints, flags: pd.DataFrame) -> pd.DataFrame:
    """Join brief constraints (is_doc_proof, hardness) to canonical flag rows on constraint_id."""
    f = flags.set_index("constraint_id")
    rows = []
    for c in constraints:
        r = f.loc[c.constraint_id] if c.constraint_id in f.index else None
        rows.append({
            "constraint_id": c.constraint_id, "attribute": c.attribute,
            "hardness": c.hardness.value if hasattr(c.hardness, "value") else str(c.hardness),
            "constraint_category": c.constraint_category,
            "matchability": _flag_str(r["matchability"]) if r is not None else "NA",
            "indiamart_matchable": is_true(r["indiamart_matchable"]) if r is not None else False,
            "administrative_only": is_true(r["administrative_only"]) if r is not None else False,
            "is_doc_proof": bool(c.is_doc_proof),
            "joined": r is not None,
        })
    return pd.DataFrame(rows)


def flag_facts(flags: pd.DataFrame, true_means: str = "HARD") -> pd.DataFrame:
    """Counts of HARD/SOFT x matchability x indiamart_matchable x administrative_only."""
    d = pd.DataFrame({
        "hardness": [(true_means if is_true(g) else ("SOFT" if true_means == "HARD" else "HARD"))
                     for g in flags["golden_param"]],
        "matchability": [_flag_str(x) for x in flags["matchability"]],
        "indiamart_matchable": [_flag_str(x) for x in flags["indiamart_matchable"]],
        "administrative_only": [_flag_str(x) for x in flags["administrative_only"]],
    })
    return (d.groupby(list(d.columns)).size().rename("n").reset_index()
            .sort_values(["hardness", "matchability", "indiamart_matchable", "administrative_only"])
            .reset_index(drop=True))


# ------------------------------------------------------------------ shares
def status_shares(cells: pd.DataFrame, status_col: str = "status") -> pd.DataFrame:
    """One row per constraint: share of candidates per status (sums to 1) + aligned_share, n."""
    if cells.empty:
        return pd.DataFrame(columns=["constraint_id", "n", *STATUSES, "aligned_share"])
    ct = pd.crosstab(cells["constraint_id"], cells[status_col])
    for s in STATUSES:
        if s not in ct.columns:
            ct[s] = 0
    n = ct.sum(axis=1)
    out = ct[STATUSES].div(n, axis=0)
    out["n"] = n
    out["aligned_share"] = cells.groupby("constraint_id")["aligned"].mean()
    return out.reset_index()[["constraint_id", "n", *STATUSES, "aligned_share"]]


def hard_table(cells: pd.DataFrame, cons: pd.DataFrame) -> pd.DataFrame:
    """Per-HARD-constraint table: flags + status shares + aligned share."""
    hard = cons[cons["hardness"] == "HARD"]
    sh = status_shares(cells)
    return hard.merge(sh, on="constraint_id", how="left").reset_index(drop=True)


def breakdown(tbl: pd.DataFrame, by: str) -> pd.DataFrame:
    """Mean status shares across HARD constraints grouped by a flag column."""
    g = tbl.groupby(by)
    out = g[STATUSES + ["aligned_share"]].mean()
    out.insert(0, "n_constraints", g.size())
    return out.reset_index()


# ------------------------------------------------------------------ feasibility variants
def variant_status(cells: pd.DataFrame, cons: pd.DataFrame, variant: str) -> pd.Series:
    """Effective status string per cell under what-if variant A/B/C/D (see module docstring).

    Rules apply only to cells whose RAW status is UNKNOWN (FAIL means evidence exists; PENDING stays).
    """
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    st = cells["status"].copy()
    if variant == "A":
        return st
    c = cons.set_index("constraint_id")
    hard = cells["constraint_id"].map(c["hardness"]).eq("HARD")
    doc = cells["constraint_id"].map(c["is_doc_proof"]).fillna(False).astype(bool)
    mat = cells["constraint_id"].map(c["indiamart_matchable"]).fillna(False).astype(bool)
    adm = cells["constraint_id"].map(c["administrative_only"]).fillna(False).astype(bool)
    unk = hard & cells["status"].eq("UNKNOWN")

    # --- variant B
    st[unk & adm] = NA
    m = unk & ~adm & doc & ~mat
    st[m] = "PENDING_VERIFICATION"
    st[unk & ~adm & ~doc & ~mat] = NA
    if variant == "B":
        return st

    # --- variant C: coverage-based additions (computed from RAW cells)
    all_unknown = cells.groupby("constraint_id")["status"].apply(lambda s: bool((s == "UNKNOWN").all()))
    full_unk = cells["constraint_id"].map(all_unknown).fillna(False).astype(bool)
    rem = unk & st.eq("UNKNOWN")
    st[rem & doc] = "PENDING_VERIFICATION"                      # (ii)
    st[rem & ~doc & full_unk] = NA                              # (i)
    if variant == "C":
        return st

    # --- variant D: every remaining HARD UNKNOWN is non-blocking
    st[hard & st.eq("UNKNOWN")] = NBU
    return st


def blocked_matrix(cells: pd.DataFrame, cons: pd.DataFrame, status: pd.Series) -> pd.DataFrame:
    """Boolean candidate x constraint matrix of blocking cells (HARD FAIL/UNKNOWN, SOFT FAIL)."""
    hard = cells["constraint_id"].map(cons.set_index("constraint_id")["hardness"]).eq("HARD")
    blocks = (hard & status.isin(["FAIL", "UNKNOWN"])) | (~hard & status.eq("FAIL"))
    m = pd.DataFrame({"product_id": cells["product_id"], "constraint_id": cells["constraint_id"],
                      "b": blocks})
    return m.pivot_table(index="product_id", columns="constraint_id", values="b", aggfunc="max").astype(bool)


def feasible_ids(cells: pd.DataFrame, cons: pd.DataFrame, variant: str) -> list[str]:
    if cells.empty:
        return []
    bm = blocked_matrix(cells, cons, variant_status(cells, cons, variant))
    return sorted(bm.index[~bm.any(axis=1)])


def feasible_counts(cells: pd.DataFrame, cons: pd.DataFrame, min_shortlist: int,
                    n_candidates: Optional[int] = None) -> pd.DataFrame:
    """Rows A-D: feasible count, share of pool, meets min_shortlist."""
    total = n_candidates if n_candidates is not None else cells["product_id"].nunique()
    rows = []
    for v in VARIANTS:
        n = len(feasible_ids(cells, cons, v)) if not cells.empty else total
        rows.append({"variant": v, "feasible": n, "pool": total,
                     "share": (n / total) if total else 0.0, "meets_min_shortlist": n >= min_shortlist})
    return pd.DataFrame(rows)


def bottlenecks(cells: pd.DataFrame, cons: pd.DataFrame, top: int = 5) -> pd.DataFrame:
    """Rank HARD constraints under variant A by (only-blocker count desc, UNKNOWN share desc).

    ``driver`` is FAIL when FAIL cells outnumber UNKNOWN cells among the blocking cells, else UNKNOWN.
    """
    if cells.empty:
        return pd.DataFrame()
    bm = blocked_matrix(cells, cons, cells["status"])
    hard_ids = cons.loc[cons["hardness"] == "HARD", "constraint_id"]
    hard_cols = [c for c in hard_ids if c in bm.columns]
    hb = bm[hard_cols]
    only = hb[hb.sum(axis=1) == 1].sum(axis=0)
    sh = status_shares(cells).set_index("constraint_id")
    out = cons[cons["constraint_id"].isin(hard_cols)][["constraint_id", "attribute"]].copy()
    out["only_blocker_n"] = out["constraint_id"].map(only).fillna(0).astype(int)
    out["UNKNOWN"] = out["constraint_id"].map(sh["UNKNOWN"])
    out["FAIL"] = out["constraint_id"].map(sh["FAIL"])
    out["driver"] = ["FAIL-driven" if f > u else "UNKNOWN-driven" for f, u in zip(out["FAIL"], out["UNKNOWN"])]
    out = out.sort_values(["only_blocker_n", "UNKNOWN", "constraint_id"],
                          ascending=[False, False, True]).reset_index(drop=True)
    return out.head(top)


def residual_fails(cells: pd.DataFrame, cons: pd.DataFrame, sample: int = 2) -> pd.DataFrame:
    """HARD constraints with real FAIL cells (these survive every variant): n_fail + sample evidence."""
    hard = cons[cons["hardness"] == "HARD"].set_index("constraint_id")
    f = cells[cells["status"].eq("FAIL") & cells["constraint_id"].isin(hard.index)]
    rows = []
    for cid, g in f.groupby("constraint_id"):
        ev = [str(e) for e in g["evidence"].dropna().unique()][:sample]
        rows.append({"constraint_id": cid, "attribute": hard.loc[cid, "attribute"], "n_fail": len(g),
                     "sample_evidence": "; ".join(ev)})
    cols = ["constraint_id", "attribute", "n_fail", "sample_evidence"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows).sort_values("n_fail", ascending=False).reset_index(drop=True)


def rank_bids(counts: dict[str, pd.DataFrame], variant: str) -> pd.DataFrame:
    """Bids ordered by feasible count under a variant (desc), ties by bid id; dense rank column."""
    rows = [{"bid": b, "feasible": int(df.loc[df.variant == variant, "feasible"].iloc[0])}
            for b, df in counts.items()]
    d = pd.DataFrame(rows).sort_values(["feasible", "bid"], ascending=[False, True]).reset_index(drop=True)
    d["rank"] = d["feasible"].rank(method="min", ascending=False).astype(int)
    return d


# ------------------------------------------------------------------ section 8: D046 / D047 what-ifs
import re
import statistics

HARD_, SOFT_ = "HARD", "SOFT"
_STD_RE = re.compile(r"(?<![A-Za-z])(IS|ISO|IEC|BIS|EN|ASTM|DIN|BS)(?![A-Za-z])[\s:/\-]*(?:NO\.?\s*)?(\d{3,6})")
MERGED_PREFIX = "MERGED::"


def std_keys(text: object) -> set[str]:
    """Standard identifiers in free text, normalised: 'IS:8034' / 'IS 8034' / 'is-8034' -> 'IS 8034'.

    Prefix must be upper-case (avoids the English word 'is'/'en'); 3-6 digits; the year suffix
    (':2012') and part numbers are not part of the key.
    """
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return set()
    return {f"{m.group(1)} {m.group(2)}" for m in _STD_RE.finditer(str(text))}


def bid_meta(brief_constraints, flags: pd.DataFrame) -> pd.DataFrame:
    """constraint_id, raw_text, buyer_value (brief) + requirement_* (canonical flag rows)."""
    f = flags.set_index("constraint_id")
    rows = []
    for c in brief_constraints:
        r = f.loc[c.constraint_id] if c.constraint_id in f.index else None
        g = (lambda k: None if r is None or pd.isna(r[k]) else str(r[k]))
        rows.append({"constraint_id": c.constraint_id, "raw_text": c.raw_text, "buyer_value": c.buyer_value,
                     "requirement_description": g("requirement_description"),
                     "requirement_group_id": g("requirement_group_id"),
                     "requirement_group_type": g("requirement_group_type")})
    return pd.DataFrame(rows, columns=["constraint_id", "raw_text", "buyer_value", "requirement_description",
                                       "requirement_group_id", "requirement_group_type"])


def standard_groups(meta: pd.DataFrame, cons: pd.DataFrame) -> dict[str, list[str]]:
    """D046 operational grouping of ACTIVE constraints by shared standard id.

    Keys come from raw_text, buyer_value, requirement_description. A constraint is assigned to ONE key
    (the key shared by most active constraints; ties by key name) so groups partition. A group needs
    >= 2 members and >= 1 HARD member.
    """
    hard = cons.set_index("constraint_id")["hardness"]
    m = meta[meta["constraint_id"].isin(hard.index)]
    ck = {r.constraint_id: std_keys(r.raw_text) | std_keys(r.buyer_value) | std_keys(r.requirement_description)
          for r in m.itertuples()}
    support: dict[str, int] = {}
    for ks in ck.values():
        for k in ks:
            support[k] = support.get(k, 0) + 1
    groups: dict[str, list[str]] = {}
    for cid in sorted(ck):
        if ck[cid]:
            best = sorted(ck[cid], key=lambda k: (-support[k], k))[0]
            groups.setdefault(best, []).append(cid)
    return {k: v for k, v in groups.items() if len(v) >= 2 and any(hard[c] == HARD_ for c in v)}


def span_groups(meta: pd.DataFrame, cons: pd.DataFrame) -> dict[str, list[str]]:
    """Sensitivity: group ACTIVE constraints with identical (whitespace-normalised) non-empty raw_text."""
    hard = cons.set_index("constraint_id")["hardness"]
    groups: dict[str, list[str]] = {}
    for r in meta[meta["constraint_id"].isin(hard.index)].itertuples():
        t = None if r.raw_text is None or (isinstance(r.raw_text, float) and pd.isna(r.raw_text)) \
            else " ".join(str(r.raw_text).split())
        if t:
            groups.setdefault(t, []).append(r.constraint_id)
    return {f"span{i}": v for i, (_, v) in enumerate(sorted(groups.items()))
            if len(v) >= 2 and any(hard[c] == HARD_ for c in v)}


def merge_status(statuses: Sequence[str]) -> str:
    """Merged cell precedence: FAIL if any FAIL; else PENDING_VERIFICATION if any PASS/PENDING; else UNKNOWN."""
    s = set(statuses)
    if "FAIL" in s:
        return "FAIL"
    if s & {"PASS", "PENDING_VERIFICATION"}:
        return "PENDING_VERIFICATION"
    return "UNKNOWN"


def apply_grouping(cells: pd.DataFrame, cons: pd.DataFrame,
                   groups: dict[str, list[str]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Replace the members of each group by one merged doc-proof constraint (HARD if any member HARD)."""
    if not groups:
        return cells, cons
    hard = cons.set_index("constraint_id")["hardness"]
    member_ids = {c for v in groups.values() for c in v}
    keep_cells = cells[~cells["constraint_id"].isin(member_ids)]
    new_cells, new_cons = [], []
    for k, mem in groups.items():
        mid = MERGED_PREFIX + k
        sub = cells[cells["constraint_id"].isin(mem)]
        for pid, g in sub.groupby("product_id"):
            new_cells.append({"product_id": pid, "constraint_id": mid, "status": merge_status(list(g["status"])),
                              "aligned": bool(g["aligned"].any()), "evidence": None})
        new_cons.append({"constraint_id": mid, "attribute": k,
                         "hardness": HARD_ if any(hard[c] == HARD_ for c in mem) else SOFT_,
                         "is_doc_proof": True})
    c2 = pd.concat([keep_cells, pd.DataFrame(new_cells, columns=cells.columns)], ignore_index=True)
    k2 = pd.concat([cons[~cons["constraint_id"].isin(member_ids)], pd.DataFrame(new_cons)], ignore_index=True)
    return c2, k2


def smoothed_pass_rates(cells: pd.DataFrame) -> pd.Series:
    """p_c per constraint = (k + 0.5*2) / (n + 2); k = PASS|PENDING cells, n = non-UNKNOWN cells."""
    n = cells[cells["status"] != "UNKNOWN"].groupby("constraint_id").size()
    k = cells[cells["status"].isin(["PASS", "PENDING_VERIFICATION"])].groupby("constraint_id").size()
    ids = cells["constraint_id"].unique()
    n = n.reindex(ids).fillna(0)
    k = k.reindex(ids).fillna(0)
    return (k + 1.0) / (n + 2.0)


CLASSES = ["verified", "conditional", "soft_only_blocked", "conditional_and_soft_blocked", "excluded"]


def classify_candidates(cells: pd.DataFrame, cons: pd.DataFrame) -> pd.DataFrame:
    """D047 classes per candidate (index product_id): cls, hasU, sF, p_unknown, verification_required."""
    hard = cells["constraint_id"].map(cons.set_index("constraint_id")["hardness"]).eq(HARD_)
    st = cells["status"]
    pc = smoothed_pass_rates(cells)
    d = pd.DataFrame({"product_id": cells["product_id"], "constraint_id": cells["constraint_id"],
                      "hF": hard & st.eq("FAIL"), "hU": hard & st.eq("UNKNOWN"), "sF": ~hard & st.eq("FAIL")})
    agg = d.groupby("product_id")[["hF", "hU", "sF"]].max()
    out = []
    for pid, r in agg.iterrows():
        if r.hF:
            cls = "excluded"
        elif r.hU:
            cls = "conditional_and_soft_blocked" if r.sF else "conditional"
        else:
            cls = "soft_only_blocked" if r.sF else "verified"
        out.append((pid, cls, bool(r.hU), bool(r.sF)))
    res = pd.DataFrame(out, columns=["product_id", "cls", "hasU", "sF"]).set_index("product_id")
    u = d[d["hU"]]
    req = u.groupby("product_id")["constraint_id"].apply(list)
    res["verification_required"] = [req.get(p, []) for p in res.index]
    res["p_unknown"] = [float(pd.Series([pc[c] for c in req.get(p, [])]).prod()) if p in req.index else 1.0
                        for p in res.index]
    return res


ELIGIBLE = {"V0": ["verified"], "V1": ["verified"],
            "V2": ["verified", "conditional"], "V3": ["verified", "conditional"]}
SOFT_POOL = {"V0": ["soft_only_blocked"], "V1": ["soft_only_blocked"],
             "V2": ["soft_only_blocked", "conditional_and_soft_blocked"],
             "V3": ["soft_only_blocked", "conditional_and_soft_blocked"]}


def variant_summary(cells: pd.DataFrame, cons: pd.DataFrame, min_shortlist: int, variant: str) -> dict:
    """Counts per class, trigger flags and distinct SOFT blockers for one variant's (cells, cons)."""
    cl = classify_candidates(cells, cons)
    cnt = {c: int((cl["cls"] == c).sum()) for c in CLASSES}
    soft_ids = set(cons.loc[cons["hardness"] == SOFT_, "constraint_id"])
    pool = set(cl.index[cl["cls"].isin(SOFT_POOL[variant])])
    blockers = set(cells.loc[cells["product_id"].isin(pool) & cells["status"].eq("FAIL")
                             & cells["constraint_id"].isin(soft_ids), "constraint_id"])
    elig = sum(cnt[c] for c in ELIGIBLE[variant])
    pool_n = sum(cnt[c] for c in SOFT_POOL[variant])
    would_be = (cnt["verified"] + cnt["conditional"] < min_shortlist) and \
        (cnt["soft_only_blocked"] + cnt["conditional_and_soft_blocked"] >= 1)
    return {**cnt, "eligible": elig, "triggers": elig < min_shortlist and pool_n >= 1,
            "would_be_trigger": would_be, "soft_blockers": len(blockers), "_cl": cl}


def p_unknown_stats(cl: pd.DataFrame, cls: Sequence[str] = ("conditional",)) -> dict:
    v = cl.loc[cl["cls"].isin(cls), "p_unknown"].tolist()
    if not v:
        return {"n": 0, "min": None, "median": None, "max": None, "ge_0.01": 0, "ge_0.05": 0, "ge_0.10": 0,
                "med_unknown_cells": None}
    nu = cl.loc[cl["cls"].isin(cls), "verification_required"].map(len).tolist()
    return {"n": len(v), "med_unknown_cells": statistics.median(nu), "min": min(v), "median": statistics.median(v), "max": max(v),
            "ge_0.01": sum(x >= 0.01 for x in v), "ge_0.05": sum(x >= 0.05 for x in v),
            "ge_0.10": sum(x >= 0.10 for x in v)}


def group_id_table(meta: pd.DataFrame, cons: pd.DataFrame) -> dict:
    """What requirement_group_id is: sizes/types and whether a group shares one standard id."""
    sizes = meta.groupby("requirement_group_id").size()
    types = sorted(meta["requirement_group_type"].dropna().unique())
    multi_std = 0
    for gid, g in meta.groupby("requirement_group_id"):
        ks = [std_keys(r.raw_text) | std_keys(r.buyer_value) | std_keys(r.requirement_description)
              for r in g.itertuples()]
        shared = set.intersection(*ks) if ks and all(ks) else set()
        multi_std += bool(shared and len(g) >= 2)
    return {"groups": int(len(sizes)), "sizes": sorted(sizes.tolist(), reverse=True),
            "types": ", ".join(types) or "-", "groups_sharing_one_standard": multi_std,
            "rows_without_gid": int(meta["requirement_group_id"].isna().sum())}


def _f(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.2e}"


def render_section8(data: dict[str, dict], min_sl: int) -> str:
    L = ["## 8. What-if: D046 grouping and D047 conditional eligibility (measured, NOT applied)", "",
         "Report-layer only: no contract, setting or matching code is changed. Active constraints only "
         "(reframe-dropped excluded). Cells are the raw `check_view` cells (no normalise step), so UNKNOWN is an "
         "upper bound.", "",
         "### 8.1 What `requirement_group_id` really is", ""]
    rows = []
    for b, d in data.items():
        g = group_id_table(d["meta"], d["cons"])
        rows.append({"bid": b, "rows": len(d["meta"]), "groups": g["groups"],
                     "sizes": ",".join(map(str, g["sizes"])), "types": g["types"],
                     "groups_sharing_one_standard_id": g["groups_sharing_one_standard"],
                     "rows_without_group": g["rows_without_gid"]})
    L += [md_table(pd.DataFrame(rows)),
          "`requirement_description` is `<type>:<GROUP_ID>` and `requirement_group_id` is the item/product-line "
          "key the clause was extracted under (e.g. `product:SUBMERSIBLE_PUMP_01`). A group therefore holds every "
          "constraint of one line item, regardless of topic; it does NOT identify clauses of one standard "
          "(column `groups_sharing_one_standard_id` counts groups whose members all share one standard id). "
          "It cannot be used for D046 as is.", "",
          "### 8.2 Definitions", "",
          "- **Standard id**: regex over `raw_text`, `buyer_value`, `requirement_description`; upper-case prefix "
          "IS|ISO|IEC|BIS|EN|ASTM|DIN|BS, optional `:`/space/`-`/`/`, 3-6 digits; `IS:8034` and `IS 8034` -> key "
          "`IS 8034` (year suffix and part numbers ignored). A constraint with several ids joins the id shared "
          "by most active constraints.",
          "- **Group (primary)**: same key, >= 2 members, >= 1 HARD. **Sensitivity**: identical whitespace-"
          "normalised `raw_text` (one extracted sentence), >= 2 members, >= 1 HARD.",
          "- **Merged cell** precedence: FAIL if any member FAIL; else PENDING_VERIFICATION if any member PASS or "
          "PENDING_VERIFICATION (a claim exists); else UNKNOWN. **Merged hardness**: HARD if any member HARD "
          "(so a SOFT FAIL member can become a HARD FAIL).",
          "- **Classes** (mutually exclusive): excluded = >= 1 HARD FAIL; else hasU = >= 1 HARD UNKNOWN, sF = >= 1 "
          "SOFT FAIL: verified (not hasU, not sF), conditional (hasU, not sF), soft_only_blocked (not hasU, sF), "
          "conditional_and_soft_blocked (hasU, sF).",
          "- **Eligible classes per variant**: V0 and V1: verified only (conditional is a would-be class, "
          "current rule blocks HARD UNKNOWN). V2 and V3: verified + conditional. UNKNOWN is never treated as PASS: "
          "conditional means *cannot be excluded, must be verified*.",
          f"- `negotiation_triggers_naturally` = eligible < min_shortlist ({min_sl}) AND soft pool >= 1, where soft "
          "pool = soft_only_blocked (V0, V1) or soft_only_blocked + conditional_and_soft_blocked (V2, V3). "
          "`would_be` applies the literal formula (verified + conditional < min_shortlist AND soft_only + "
          "cond_and_soft >= 1) to every variant.",
          "- V0 current; V1 = D046 only; V2 = D047 only; V3 = D046 + D047.", ""]

    L += ["### 8.3 D046 groups found", ""]
    grows, gdetail = [], []
    for b, d in data.items():
        for label, fn in (("standard-id", standard_groups), ("same-raw_text", span_groups)):
            gr = fn(d["meta"], d["cons"])
            hb = int((d["cons"]["hardness"] == HARD_).sum())
            c2, k2 = apply_grouping(d["cells"], d["cons"], gr)
            ha = int((k2["hardness"] == HARD_).sum())
            grows.append({"bid": b, "grouping": label, "groups": len(gr),
                          "members_merged": sum(len(v) for v in gr.values()),
                          "HARD_before": hb, "HARD_after": ha})
            if label == "standard-id":
                gdetail += [f"- {b}: " + (", ".join(f"`{k}` ({len(v)})" for k, v in gr.items()) or "none")]
    L += [md_table(pd.DataFrame(grows)), "Standard-id groups (member counts):", ""] + gdetail + [""]

    L += ["### 8.4 Per bid x variant", ""]
    rows, pu_rows = [], []
    for b, d in data.items():
        gr = standard_groups(d["meta"], d["cons"])
        c1, k1 = apply_grouping(d["cells"], d["cons"], gr)
        for v, (c, k) in {"V0": (d["cells"], d["cons"]), "V1": (c1, k1),
                          "V2": (d["cells"], d["cons"]), "V3": (c1, k1)}.items():
            s = variant_summary(c, k, min_sl, v)
            rows.append({"bid": b, "variant": v, "candidates": d["n_cand"],
                         **{x: s[x] for x in CLASSES}, "eligible": s["eligible"],
                         "triggers_naturally": "yes" if s["triggers"] else "no",
                         "would_be": "yes" if s["would_be_trigger"] else "no",
                         "distinct_soft_blockers": s["soft_blockers"]})
            if v in ("V2", "V3"):
                st = p_unknown_stats(s["_cl"])
                st2 = p_unknown_stats(s["_cl"], ("conditional", "conditional_and_soft_blocked"))
                for lab, x in (("conditional", st), ("conditional + cond_and_soft", st2)):
                    pu_rows.append({"bid": b, "variant": v, "pool": lab, "n": x["n"], "min": _f(x["min"]),
                                    "median": _f(x["median"]), "max": _f(x["max"]),
                                    "median_HARD_UNKNOWN_cells": x["med_unknown_cells"], ">=0.01": x["ge_0.01"],
                                    ">=0.05": x["ge_0.05"], ">=0.10": x["ge_0.10"]})
    tbl = pd.DataFrame(rows)
    L += [md_table(tbl), "", "Sensitivity: V1/V3 with grouping by identical `raw_text`.", ""]
    srows = []
    for b, d in data.items():
        gr = span_groups(d["meta"], d["cons"])
        c1, k1 = apply_grouping(d["cells"], d["cons"], gr)
        for v in ("V1", "V3"):
            s = variant_summary(c1, k1, min_sl, v)
            srows.append({"bid": b, "variant": v, "groups": len(gr), **{x: s[x] for x in CLASSES},
                          "eligible": s["eligible"], "triggers_naturally": "yes" if s["triggers"] else "no",
                          "distinct_soft_blockers": s["soft_blockers"]})
    L += [md_table(pd.DataFrame(srows)), "",
          "### 8.5 p_unknown distribution (UNKNOWN factors only)", "",
          "p_c per HARD UNKNOWN cell = (k + 1) / (n + 2), k = candidates PASS/PENDING on the constraint, "
          "n = candidates with a non-UNKNOWN status on it (n=0 -> 0.5). p_unknown = product over the "
          "candidate's HARD UNKNOWN cells; it ignores the PASS cells' own confidence factors.", "",
          md_table(pd.DataFrame(pu_rows)), ""]

    # reading
    nat = {v: sorted(set(tbl[(tbl.variant == v) & (tbl.triggers_naturally == "yes")]["bid"])) for v in
           ("V0", "V1", "V2", "V3")}
    pu = pd.DataFrame(pu_rows)
    cond = pu[(pu["pool"] == "conditional") & (pu.variant == "V2")]
    tot_cond = int(cond["n"].sum())
    tot_ge05 = int(cond[">=0.05"].sum())
    tot_lt01 = tot_cond - int(cond[">=0.01"].sum())
    L += ["### 8.6 Plain-language reading (computed counts)", "",
          *[f"- {v}: bundle negotiation triggers naturally in {len(nat[v])} of {len(data)} bids"
            + (f" ({', '.join(x.split('/')[-1] for x in nat[v])})." if nat[v] else ".") for v in nat],
          f"- Across the 6 bids V2 has {tot_cond} conditional candidates; {tot_ge05} have p_unknown >= 0.05 and "
          f"{tot_lt01} have p_unknown < 0.01. When most conditional candidates sit near zero, a "
          "RECOMMENDED_CONDITIONAL outcome would mostly list candidates that are unlikely to survive "
          "verification, i.e. nearly meaningless without a floor.",
          "- Conditional never means PASS: a conditional candidate is only one that cannot be excluded.", ""]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ markdown
def md_table(df: pd.DataFrame, pct: Iterable[str] = ()) -> str:
    pct = set(pct)
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


# ------------------------------------------------------------------ driver
class RequestCounter:
    """Counts (and caps) live HTTP requests by wrapping ``httpx.post`` in the LLM client module."""

    def __init__(self, cap: int) -> None:
        self.cap, self.n = cap, 0

    def install(self) -> None:
        import gtgen.llm.client as cl
        orig = cl.httpx.post

        def counted(*a, **k):
            if self.n >= self.cap:
                raise RuntimeError(f"HTTP request cap {self.cap} reached")
            self.n += 1
            return orig(*a, **k)
        cl.httpx.post = counted


def _pct_cols() -> list[str]:
    return STATUSES + ["aligned_share", "share"]


def collect_bid(bid_id: str, settings: dict, runners, flags_all: pd.DataFrame) -> dict:
    from gtgen.cli import prepare_bid
    from gtgen.contracts import FramingDecision
    prep = prepare_bid(bid_id, settings, runners)
    al = {a.buyer_attribute: a for a in prep.alignments}
    cells = build_cell_frame(prep.active_constraints, prep.catalogue.candidates, al,
                             unverified_factor=settings["ranking"]["unverified_text_factor"],
                             rel_tol=settings["negotiation"]["bundle_search"]["numeric_eq_rel_tolerance"])
    flags = flags_all[flags_all["bid_id"] == bid_id]
    all_cons = build_constraint_frame(prep.brief.constraints, flags)
    dropped = {c.constraint_id for c in prep.brief.constraints if c.framing_decision == FramingDecision.DROP}
    cons = all_cons[~all_cons["constraint_id"].isin(dropped)].reset_index(drop=True)
    meta = bid_meta(prep.brief.constraints, flags)
    return {"bid": bid_id, "cells": cells, "cons": cons, "all_cons": all_cons, "dropped": dropped,
            "meta": meta,
            "n_cand": len(prep.catalogue.candidates), "flags": flags,
            "n_total_constraints": len(prep.brief.constraints)}


def render(data: dict[str, dict], flags_all: pd.DataFrame, settings: dict, requests_used: int,
           live: bool, note: str = "") -> str:
    true_means = settings["golden_param_true_means"]
    min_sl = int(settings["negotiation"]["min_shortlist"])
    L: list[str] = []
    L += ["# Coverage report: what blocks HARD constraints", "",
          f"_Generated by `python -m gtgen.data.coverage` on {_dt.date.today().isoformat()}; "
          "all numbers below are computed, none hand-written._", ""]
    L += ["## 1. Method, limitations, requests", "",
          "- Per bid: `prepare_bid` (reframe, doc_classify, align; cache-backed) then `compliance_matrix` logic "
          "(via `check_view`) on the ACTIVE constraints (reframe-dropped constraints are excluded).",
          "- Cells are computed WITHOUT the LLM `normalise` step: free-text evidence (description, brochure) is "
          "not normalised, so UNKNOWN shares are an UPPER BOUND and feasible counts a LOWER BOUND.",
          "- Flags (`matchability`, `indiamart_matchable`, `administrative_only`) come from "
          "`final_constraints.parquet` joined on `constraint_id`; `is_doc_proof` from the doc_classify step.",
          "- `aligned share` = share of candidates with any aligned candidate-side evidence "
          "(usable parsed value, or an attribute/document claim).",
          "- NOT_ASSESSABLE / NON_BLOCKING_UNKNOWN are report-layer strings only; no contract or rule is changed.",
          f"- `golden_param_true_means` = {true_means}; `min_shortlist` = {min_sl}.",
          f"- Live HTTP requests used by this run: **{requests_used}** (cap 80, enforced in-process). LLM mode live: {live}. {note}",
          f"- Bids: {', '.join(data)}.", ""]

    # 2 flag facts
    L += ["## 2. Flag facts", "", f"### All {len(flags_all)} constraint rows", "",
          md_table(flag_facts(flags_all, true_means))]
    six = flags_all[flags_all["bid_id"].isin(list(data))]
    L += [f"### The {len(data)} bids ({len(six)} constraint rows)", "", md_table(flag_facts(six, true_means))]
    ff = flag_facts(flags_all, true_means)
    hard_ff = ff[ff.hardness == "HARD"]
    n_hard = int(hard_ff.n.sum())
    n_discr = int(hard_ff[(hard_ff.indiamart_matchable != "True") | (hard_ff.administrative_only == "True")].n.sum())
    L += [f"**Verdict.** Of {n_hard} HARD rows, {n_discr} have `indiamart_matchable` false or "
          f"`administrative_only` true. "
          + ("Flag-based 'not assessable' logic would therefore apply to ZERO HARD constraints."
             if n_discr == 0 else "Flag-based logic applies only to those rows."), ""]

    # 3 per-bid tables
    L += ["## 3. Per-bid HARD constraint tables", ""]
    for b, d in data.items():
        tbl = hard_table(d["cells"], d["cons"])
        dh = sum(1 for cid in d["dropped"] if d["all_cons"].set_index("constraint_id").loc[cid, "hardness"] == "HARD")
        tot_h = int((d["all_cons"]["hardness"] == "HARD").sum())
        L += [f"### {b}", "",
              f"{d['n_cand']} candidates; {d['n_total_constraints']} constraints ({tot_h} HARD); "
              f"reframe dropped {len(d['dropped'])} constraints, of which **{dh} HARD** "
              f"(active HARD: {tot_h - dh}).", "",
              md_table(tbl[["attribute", "constraint_category", "matchability", "indiamart_matchable",
                            "administrative_only", "is_doc_proof", *STATUSES, "aligned_share"]],
                       pct=_pct_cols())]
        unj = int((~d["all_cons"]["joined"]).sum())
        if unj:
            L += [f"_Join orphans: {unj} brief constraints without a flag row._", ""]

    # 4 breakdowns
    L += ["## 4. Mean status shares across HARD constraints, by flag", ""]
    all_tbl = []
    for b, d in data.items():
        t = hard_table(d["cells"], d["cons"])
        t["bid"] = b
        all_tbl.append(t)
    big = pd.concat(all_tbl, ignore_index=True) if all_tbl else pd.DataFrame()
    for by in FLAG_COLS:
        L += [f"### by `{by}`", "", "Overall:", "", md_table(breakdown(big, by), pct=_pct_cols())]
        for b in data:
            L += [f"{b}:", "", md_table(breakdown(big[big.bid == b], by), pct=_pct_cols())]

    # 5 bottlenecks
    L += ["## 5. Top bottleneck HARD constraints per bid (variant A)", "",
          "Ranked by (a) number of candidates for which the constraint is the ONLY blocking HARD cell, "
          "then (b) UNKNOWN share. `driver`: FAIL-driven = contradicting evidence exists; "
          "UNKNOWN-driven = no evidence.", ""]
    for b, d in data.items():
        L += [f"### {b}", "", md_table(bottlenecks(d["cells"], d["cons"]), pct=["UNKNOWN", "FAIL"])]

    # 6 feasible counts
    L += ["## 6. Feasible counts under four variants", ""]
    for v in VARIANTS:
        L.append(f"- **{VARIANT_LABEL[v]}**")
    L += ["",
          "Interpretation of rule 2: applied only to cells whose raw status is UNKNOWN (FAIL = evidence "
          "exists, stays FAIL; raw PENDING stays). Order for raw-UNKNOWN HARD cells in B: administrative_only "
          "-> NOT_ASSESSABLE; doc-proof and not matchable -> PENDING_VERIFICATION; non-doc and not "
          "matchable -> NOT_ASSESSABLE; else UNKNOWN. C adds (i) non-doc constraints UNKNOWN for 100% of the "
          "pool -> NOT_ASSESSABLE and (ii) doc-proof UNKNOWN -> PENDING_VERIFICATION. D treats all remaining "
          "HARD UNKNOWN as non-blocking. SOFT FAIL blocks in every variant.", ""]
    counts = {b: feasible_counts(d["cells"], d["cons"], min_sl, d["n_cand"]) for b, d in data.items()}
    rows = []
    for b, cdf in counts.items():
        r = {"bid": b, "candidates": int(cdf["pool"].iloc[0])}
        for v in VARIANTS:
            x = cdf[cdf.variant == v].iloc[0]
            r[v] = int(x["feasible"])
            r[f"{v}>=min"] = "yes" if x["meets_min_shortlist"] else "no"
        rows.append(r)
    L += [md_table(pd.DataFrame(rows))]
    for v in ("C", "D"):
        L += [f"Ranking of bids by feasible count under {VARIANT_LABEL[v]}:", "",
              md_table(rank_bids(counts, v))]

    # 7 residual
    L += ["## 7. What stays infeasible even in D", "",
          "HARD FAIL cells carry contradicting evidence; no variant relaxes them. SOFT FAIL also blocks "
          "in every variant.", ""]
    for b, d in data.items():
        rf = residual_fails(d["cells"], d["cons"])
        feas_d = set(feasible_ids(d["cells"], d["cons"], "D"))
        st_d = variant_status(d["cells"], d["cons"], "D")
        hard = d["cells"]["constraint_id"].map(d["cons"].set_index("constraint_id")["hardness"]).eq("HARD")
        hard_fail_prod = set(d["cells"].loc[hard & st_d.eq("FAIL"), "product_id"])
        L += [f"### {b}", "",
              f"{d['n_cand']} candidates; {len(feas_d)} feasible in D; "
              f"{len(hard_fail_prod)} candidates carry at least one HARD FAIL.", "",
              md_table(rf)]
    L += [render_section8(data, min_sl)]
    return "\n".join(L) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prior-requests-note", default="",
                    help="free text about HTTP requests made by earlier (interrupted) runs that warmed the cache")
    args = ap.parse_args(argv)
    os.environ.setdefault("GTGEN_LLM_MODE", "live")
    from gtgen.cli import prepare_bid  # noqa: F401  (importing gtgen.cli loads .env)
    from gtgen.data.loaders import load_settings
    from gtgen.llm import LLMClient, LLMRunners

    logging.basicConfig(level=logging.WARNING)
    cap = 80
    counter = RequestCounter(cap)
    counter.install()
    settings = load_settings()
    runners = LLMRunners(LLMClient(settings), settings)
    flags_all = pd.read_parquet(os.path.join(settings["paths"]["canonical"], "final_constraints.parquet"))
    data = {}
    for b in BIDS:
        data[b] = collect_bid(b, settings, runners, flags_all)
        print(f"{b}: {data[b]['n_cand']} candidates, requests so far {counter.n}", file=sys.stderr)
    text = render(data, flags_all, settings, counter.n, os.environ.get("GTGEN_LLM_MODE") == "live",
                  args.prior_requests_note)
    out = "docs/coverage.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {out}; live HTTP requests: {counter.n}")


if __name__ == "__main__":
    main()
