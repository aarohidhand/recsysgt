"""Record (or replay) the pre-dialogue LLM fixtures for the pilot bid.

Usage (repo root):  GTGEN_LLM_MODE=record PYTHONPATH=src python -m gtgen.llm.record_pilot
Warm-cache reruns make 0 HTTP calls. ``--mode mock`` replays fixtures with no network.
Live HTTP requests are counted (retries included) by HTTPGate wrapping httpx.post; there is no cap (D066).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

PILOT_BID = "GEM/2019/B/242710"
PERSONA_SEEDS = (1, 2, 3)
NORMALISE_SPEC_KEYS = {
    "motor_rating": ("HP", ("Motor Horsepower", "Power (HP)", "Power", "Power Rating", "Power Rating (HP)",
                            "Horsepower", "Horse Power", "Motor Power")),
    "max_current": ("A", ("Current", "1.06 Current", "Rated Current", "Max Current")),
}
MANIFEST = Path("tests/fixtures/llm/pilot_manifest.json")


# D070/audit 11: the cumulative `.cache/pilot_http_count.json` (a pre-D066 cap bookkeeping file written only by this tool,
# which is why it disagreed with the real request count of a pilot run) is gone. The one request counter is
# ``gtgen.llm.probe.HTTPGate`` (every httpx.post attempt, retries included); per-call attempts are also in each
# provenance event (``http_attempts``).


def normalise_requests(catalogue):
    from gtgen.llm import NormaliseRequest
    reqs = []
    for target, (unit, keys) in NORMALISE_SPEC_KEYS.items():
        freq = collections.Counter()
        for c in catalogue.candidates:
            for k in keys:
                v = (c.specifications or {}).get(k)
                if isinstance(v, str) and v.strip():
                    freq[v.strip()] += 1
        limit = 36 if target == "motor_rating" else 4
        for text, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]:
            reqs.append(NormaliseRequest(text=text, target=target, canonical_unit=unit))
    return reqs


def run(bid: str = PILOT_BID, raw_dir: str = "data/raw", settings_path: str = "config/settings.yaml",
        cache_dir: str | None = None,
        write_manifest: bool = True) -> dict:
    from gtgen.data.loaders import (
        build_candidate_constraints, build_catalogue_view, build_scenario_brief, load_settings)
    from gtgen.llm import LLMClient, LLMRunners

    settings = load_settings(settings_path)
    runners = LLMRunners(LLMClient(settings, cache_dir=cache_dir), settings)
    brief = build_scenario_brief(bid, raw_dir, settings, "pilot")
    catalogue = build_catalogue_view(bid, raw_dir, "pilot")
    cands = build_candidate_constraints(bid, raw_dir)
    product_category = collections.Counter(
        c.category_name for c in catalogue.candidates if c.category_name).most_common(1)[0][0]
    summary: dict = {"bid": bid, "product_category": product_category, "steps": {}}

    def step(name, res):
        summary["steps"][name] = {"ok": res.ok, "llm_call_ids": res.llm_call_ids, "qa_flags": res.qa_flags}

    r1 = runners.reframe(product_category, brief.constraints, version=1)   # recorded pilot fixtures are reframe v1
    step("reframe", r1)
    r2 = runners.doc_classify(r1.value)
    step("doc_classify", r2)
    final = r2.value
    summary["reframe"] = [{
        "constraint_id": c.constraint_id, "attribute": c.attribute, "decision": c.framing_decision.value,
        "commercial_text": c.commercial_text, "reason": r1.extra["reasons"].get(c.constraint_id),
        "is_doc_proof": c.is_doc_proof, "doc_name": c.doc_name, "golden_param": c.golden_param} for c in final]
    cats = [c.constraint_category for c in final if c.framing_decision.value != "drop"]
    summary["personas"] = []
    for seed in PERSONA_SEEDS:
        rp = runners.persona(product_category, brief.quantity, brief.quantity_unit, brief.budget_inr,
                             cats, seed, scenario_id=f"{bid}:s{seed}")
        step(f"persona_{seed}", rp)
        if rp.value:
            prof, per = rp.value
            summary["personas"].append({"seed": seed, "company": prof.company_name, "industry": prof.industry,
                                        "city": prof.city, "tau": per.tau, "tier": per.tier.value,
                                        "counter_band": per.counter_band, "patience": per.patience_turns,
                                        "max_concessions": per.max_concessions_per_turn,
                                        "tolerance": per.category_tolerance, "weights": per.ranking_weights})
    ra = runners.align(bid, [c for c in final if c.framing_decision.value != "drop"], cands)
    step("align", ra)
    summary["alignments"] = [a.model_dump() for a in ra.value]
    reqs = normalise_requests(catalogue)
    rn = runners.normalise(reqs)
    step("normalise", rn)
    summary["normalised"] = [
        {"text": q.text, "target": q.target, "value": (v.value if v else None), "unit": (v.unit if v else None)}
        for q, v in zip(reqs, rn.value)]
    if write_manifest:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps({
            "bid": bid, "model": settings["llm"]["model"], "backend": settings["llm"]["backend"],
            "calls": {k: v["llm_call_ids"] for k, v in summary["steps"].items()},
            "tau": {p["seed"]: p["tau"] for p in summary["personas"]},
        }, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=None, help="ignored (D066: no request cap)")
    ap.add_argument("--mode", default=None, help="override GTGEN_LLM_MODE (record|mock|live)")
    ap.add_argument("--bid", default=PILOT_BID)
    ap.add_argument("--settings", default="config/settings.yaml")
    ap.add_argument("--summary-out", default=None)
    a = ap.parse_args()
    os.environ["GTGEN_LLM_MODE"] = a.mode or os.environ.get("GTGEN_LLM_MODE", "record")
    from gtgen.llm.probe import HTTPGate
    counter = HTTPGate()
    counter.install()
    try:
        summary = run(a.bid, settings_path=a.settings)
    finally:
        print(f"HTTP requests this run (every attempt): {counter.count}; status counts: {dict(counter.status)}",
              file=sys.stderr)
    if a.summary_out:
        Path(a.summary_out).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    for k, v in summary["steps"].items():
        print(k, "ok" if v["ok"] else "QA_FLAGS", len(v["llm_call_ids"]), "calls", v["qa_flags"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
