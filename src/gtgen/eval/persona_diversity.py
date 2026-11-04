"""D060 personality-diversity audit (deterministic, CLI-free).

Trait vector per persona (all normalised to [0, 1], clipped):
  tau                      already [0, 1]
  tolerance:<category>     Persona.tolerance[category] (T_c, D053), normalised by relaxation.persona_ranges.tolerance;
                           categories are the sorted union over the run (fixed order)
  patience_rounds, decision_randomness, max_relaxations
                           normalised by relaxation.persona_ranges.<trait> as (v - lo) / (hi - lo)
A trait that is None / missing for a persona is not part of its vector.

Pairwise distance (documented label semantics): over the dimensions D present in BOTH vectors (missing tolerance
categories are therefore *excluded* for that pair, not imputed),  d = sqrt(sum_{i in D} (a_i - b_i)^2) / sqrt(|D|),
so d is in [0, 1].  A pair with |D| = 0 has no distance and is skipped.

Checks (verdicts PASS / WARN / FAIL / SKIP):
  pair_distance       FAIL if any pair < persona.diversity.min_pair_distance (near-duplicate personalities)
  company_exact       FAIL on exact duplicate company names (lower-case, whitespace-collapsed)
  style_exact         FAIL on exact duplicate speaking styles (same normalisation); reported separately
  company_near        WARN on non-identical names with similarity >= company_name_similarity
                      (similarity = max(token-set Jaccard, SequenceMatcher ratio) after dropping legal suffixes)
  industry_city       FAIL if the most common (industry, city) pair exceeds max_same_industry_city_share of the run
  strata              FAIL if a stored stratum band does not contain the persona value; WARN if a persona has empty strata
                      (legacy / pre-D060 data); PASS otherwise.  Bands are in raw trait units; tolerance bands may be a
                      single {"band": ..} entry or a {category: {"band": ..}} map.
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Mapping, Optional, Sequence

from gtgen.contracts import BuyerProfile, GTConversation, Persona
from gtgen.eval._report import FAIL, PASS, SKIP, WARN, Check, norm_text, text_similarity, worst

SCALAR_TRAITS = ("patience_rounds", "decision_randomness", "max_relaxations")
_DEFAULT_RANGES = {"max_relaxations": [0, 3], "patience_rounds": [1, 4], "decision_randomness": [0.1, 0.5],
                   "tolerance": [0.0, 5.0]}
_LEGAL = {"pvt", "ltd", "private", "limited", "llp", "inc", "co", "company", "corp", "corporation"}
_EPS = 1e-9


@dataclass
class PersonaRecord:
    scenario_id: str
    persona: Persona
    profile: BuyerProfile


@dataclass
class DiversityReport:
    n: int
    checks: list[Check]
    verdict: str
    min_distance: Optional[float]
    mean_distance: Optional[float]
    median_distance: Optional[float]
    histogram: list[int]
    bins: int
    near_duplicate_pairs: list[tuple[str, str, float]] = field(default_factory=list)
    distances: dict[tuple[str, str], float] = field(default_factory=dict)

    def summary(self) -> dict:
        return {"n": self.n, "verdict": self.verdict, "min_distance": self.min_distance,
                "mean_distance": self.mean_distance, "median_distance": self.median_distance,
                "histogram": self.histogram, "checks": {c.name: c.verdict for c in self.checks}}


def _cfg(settings: Optional[Mapping]) -> tuple[dict, dict]:
    s = settings or {}
    div = {"min_pair_distance": 0.15, "histogram_bins": 10, "company_name_similarity": 0.85,
           "max_same_industry_city_share": 0.34}
    div.update(((s.get("persona", {}) or {}).get("diversity", {}) or {}))
    ranges = dict(_DEFAULT_RANGES)
    ranges.update(((s.get("relaxation", {}) or {}).get("persona_ranges", {}) or {}))
    return div, ranges


def _unit(v: float, lo: float, hi: float) -> float:
    return 0.0 if hi <= lo else min(1.0, max(0.0, (v - lo) / (hi - lo)))


def trait_vector(p: Persona, ranges: Mapping, categories: Sequence[str] = ()) -> dict[str, float]:
    """Normalised trait vector (dimension name -> value in [0, 1])."""
    vec: dict[str, float] = {"tau": min(1.0, max(0.0, p.tau))}
    for t in SCALAR_TRAITS:
        v = getattr(p, t)
        if v is not None:
            lo, hi = ranges[t]
            vec[t] = _unit(float(v), lo, hi)
    lo, hi = ranges["tolerance"]
    for cat in categories:
        if cat in p.tolerance:
            vec[f"tolerance:{cat}"] = _unit(float(p.tolerance[cat]), lo, hi)
    return vec


def pair_distance(a: Mapping[str, float], b: Mapping[str, float]) -> Optional[float]:
    """Euclidean distance over shared dimensions divided by sqrt(#shared dims); None if no shared dimension."""
    dims = [d for d in a if d in b]
    if not dims:
        return None
    return math.sqrt(sum((a[d] - b[d]) ** 2 for d in dims)) / math.sqrt(len(dims))


def records_from_conversations(convs: Sequence[GTConversation]) -> list[PersonaRecord]:
    return [PersonaRecord(c.scenario_id, c.persona, c.buyer_profile) for c in convs]


def _name_key(s: str) -> str:
    return " ".join(s.lower().split())


def _strip_legal(s: str) -> str:
    return " ".join(t for t in norm_text(s).split() if t not in _LEGAL)


def _strata_issues(p: Persona) -> list[str]:
    issues = []
    def inside(v, band):
        return band[0] - _EPS <= v <= band[1] + _EPS
    for trait, entry in p.strata.items():
        if trait == "tolerance":
            entries = {"": entry} if isinstance(entry, dict) and "band" in entry else dict(entry or {})
            for cat, e in entries.items():
                vals = [p.tolerance[cat]] if cat else list(p.tolerance.values())
                if cat and cat not in p.tolerance:
                    continue
                if not isinstance(e, dict) or "band" not in e:
                    continue
                for v in vals:
                    if not inside(v, e["band"]):
                        issues.append(f"tolerance{':' + cat if cat else ''}={v} outside {e['band']}")
            continue
        v = getattr(p, trait, None)
        if v is None or not isinstance(entry, dict) or "band" not in entry:
            continue
        if not inside(float(v), entry["band"]):
            issues.append(f"{trait}={v} outside {entry['band']}")
    return issues


def analyse(records: Sequence[PersonaRecord], settings: Optional[Mapping] = None) -> DiversityReport:
    div, ranges = _cfg(settings)
    n = len(records)
    cats = sorted({c for r in records for c in r.persona.tolerance})
    vecs = [trait_vector(r.persona, ranges, cats) for r in records]
    dists: dict[tuple[str, str], float] = {}
    for i, j in combinations(range(n), 2):
        d = pair_distance(vecs[i], vecs[j])
        if d is not None:
            dists[(records[i].scenario_id, records[j].scenario_id)] = d
    vals = sorted(dists.values())
    bins = int(div["histogram_bins"])
    hist = [0] * bins
    for d in vals:
        hist[min(bins - 1, int(d * bins))] += 1
    near = sorted(((a, b, d) for (a, b), d in dists.items() if d < div["min_pair_distance"]),
                  key=lambda t: (t[2], t[0], t[1]))
    checks: list[Check] = []

    if not vals:
        checks.append(Check("pair_distance", SKIP, "fewer than 2 personas"))
    elif near:
        checks.append(Check("pair_distance", FAIL, f"{len(near)} pair(s) below {div['min_pair_distance']}",
                            {"pairs": near}))
    else:
        checks.append(Check("pair_distance", PASS, f"min {vals[0]:.3f} >= {div['min_pair_distance']}"))

    def dup_groups(keyf, attr_of) -> list[list[str]]:
        groups: dict[str, list[str]] = {}
        for r in records:
            k = keyf(attr_of(r))
            if k:
                groups.setdefault(k, []).append(r.scenario_id)
        return [g for g in groups.values() if len(g) > 1]

    g = dup_groups(_name_key, lambda r: r.profile.company_name)
    checks.append(Check("company_exact", FAIL if g else PASS,
                        f"{len(g)} duplicated company name(s)" if g else "all company names distinct", {"groups": g}))
    g = dup_groups(_name_key, lambda r: r.profile.style)
    checks.append(Check("style_exact", FAIL if g else PASS,
                        f"{len(g)} duplicated style(s)" if g else "all styles distinct", {"groups": g}))

    thr = div["company_name_similarity"]
    nn = []
    for i, j in combinations(range(n), 2):
        a, b = records[i].profile.company_name, records[j].profile.company_name
        if _name_key(a) == _name_key(b):
            continue
        s = text_similarity(_strip_legal(a) or a, _strip_legal(b) or b)
        if s >= thr:
            nn.append((records[i].scenario_id, records[j].scenario_id, s))
    checks.append(Check("company_near", WARN if nn else PASS,
                        f"{len(nn)} near-duplicate name pair(s) at >= {thr}" if nn else "no near-duplicate names",
                        {"pairs": nn}))

    if n:
        counts: dict[tuple[str, str], int] = {}
        for r in records:
            k = (_name_key(r.profile.industry), _name_key(r.profile.city))
            counts[k] = counts.get(k, 0) + 1
        top, cnt = max(sorted(counts.items()), key=lambda kv: kv[1])
        share = cnt / n
        bad = share > div["max_same_industry_city_share"] + _EPS and cnt > 1
        checks.append(Check("industry_city", FAIL if bad else PASS,
                            f"top pair {top} share {share:.2f} (max {div['max_same_industry_city_share']})",
                            {"top": top, "share": share}))
    else:
        checks.append(Check("industry_city", SKIP, "no personas"))

    mism, empty = [], []
    for r in records:
        if not r.persona.strata:
            empty.append(r.scenario_id)
        for msg in _strata_issues(r.persona):
            mism.append((r.scenario_id, msg))
    if mism:
        checks.append(Check("strata", FAIL, f"{len(mism)} value(s) outside stored strata", {"mismatches": mism, "empty": empty}))
    elif empty:
        checks.append(Check("strata", WARN, f"{len(empty)} persona(s) with empty strata", {"empty": empty}))
    else:
        checks.append(Check("strata", PASS if n else SKIP, "all values inside their strata"))

    return DiversityReport(
        n=n, checks=checks, verdict=worst(checks),
        min_distance=vals[0] if vals else None,
        mean_distance=statistics.fmean(vals) if vals else None,
        median_distance=statistics.median(vals) if vals else None,
        histogram=hist, bins=bins, near_duplicate_pairs=near, distances=dists)


def analyse_conversations(convs: Sequence[GTConversation], settings: Optional[Mapping] = None) -> DiversityReport:
    return analyse(records_from_conversations(convs), settings)


def render(rep: DiversityReport, width: int = 40) -> str:
    f = lambda x: "n/a" if x is None else f"{x:.3f}"  # noqa: E731
    lines = [f"Persona diversity (D060): n={rep.n}  verdict={rep.verdict}",
             f"  distance min={f(rep.min_distance)} mean={f(rep.mean_distance)} median={f(rep.median_distance)}",
             "  histogram of pairwise distances:"]
    mx = max(rep.histogram, default=0) or 1
    for k, c in enumerate(rep.histogram):
        lo, hi = k / rep.bins, (k + 1) / rep.bins
        lines.append(f"    [{lo:.2f},{hi:.2f}{']' if k == rep.bins - 1 else ')'} {'#' * round(width * c / mx):<{width}} {c}")
    lines.append("  checks:")
    for c in rep.checks:
        lines.append(f"    {c.verdict:<4} {c.name}: {c.detail}")
    for a, b, d in rep.near_duplicate_pairs:
        lines.append(f"      near-duplicate personalities: {a} ~ {b} (d={d:.3f})")
    for c in rep.checks:
        if c.name in ("company_exact", "style_exact"):
            for grp in c.data.get("groups", []):
                lines.append(f"      {c.name}: {', '.join(grp)}")
        if c.name == "company_near":
            for a, b, s in c.data.get("pairs", []):
                lines.append(f"      company_near: {a} ~ {b} (sim={s:.2f})")
        if c.name == "strata":
            for sid, msg in c.data.get("mismatches", []):
                lines.append(f"      strata: {sid}: {msg}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gtgen.eval.persona_diversity")
    ap.add_argument("--run", help="run id (reads <paths.out>/<run>/conversations.jsonl)")
    ap.add_argument("--jsonl", help="explicit conversations.jsonl path")
    ap.add_argument("--settings", default="config/settings.yaml")
    a = ap.parse_args(argv)
    if not (a.run or a.jsonl):
        ap.error("give --run or --jsonl")
    import yaml
    with open(a.settings, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    from gtgen.eval.conversations import load_conversations
    path = Path(a.jsonl) if a.jsonl else Path(settings["paths"]["out"]) / a.run / "conversations.jsonl"
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 1
    rep = analyse_conversations(load_conversations(path), settings)
    print(render(rep))
    return 1 if rep.verdict == FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
