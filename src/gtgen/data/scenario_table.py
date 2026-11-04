"""Evidence-grounded scenario table: live-call plan, run, section-9 report.

    GTGEN_LLM_MODE=mock python -m gtgen.data.scenario_table plan [--fresh] --personas 4 --out out/scenario_table
    GTGEN_LLM_MODE=live python -m gtgen.data.scenario_table run  [--fresh] --cap C   (the approved live step)
    python -m gtgen.data.scenario_table report --in out/scenario_table --out docs/coverage.md --out-section

``plan`` makes NO live request (it refuses to run unless GTGEN_LLM_MODE=mock; reframe / doc_classify / align are
served from the cache or recorded fixtures; a miss yields an UPPER-BOUND plan marked ``plan_prep_not_cached``, see
``tolerant_runners``; ``--fresh`` counts them as real calls, with the D066 deterministic-first split: items resolved by
rules / prefilter are reported separately and are not calls). D066: there is NO request cap and ``run`` never refuses for lack
of approval; ``--approved-calls`` is informational (compared with the plan's worst-case HTTP requests in a printed note) and
HTTPGate only counts requests and logs progress. A call that exhausts its retries raises ``ScenarioSkipped``; the run
continues with the next scenario/bid.
OWNER: data-steward.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import Counter
from typing import Any, Callable, Mapping, Optional, Sequence

from gtgen.data.call_plan import (  # noqa: F401  (CountingClient re-exported: it lives in call_plan now)
    CountingClient, can_plan, collect_bid_calls, dialogue_report, render_dialogue, render_table_report, stage1_totals,
    table_report,
)
from gtgen.data.scenario_build import (
    _doc_constraints, build_scenario, claim_requests, fetch_doc_claims, merged_constraints, scenario_summary,
)

DEFAULT_BIDS = ["GEM/2019/B/209245", "GEM/2019/B/231682", "GEM/2019/B/242710", "GEM/2019/B/341930",
                "GEM/2020/B/488257", "GEM/2020/B/626099"]
DEFAULT_DIR = "out/scenario_table"
SECTION_HEADER = "## 9. Evidence-grounded briefs: roles, commercial terms, initial feasibility"


class ApprovalError(RuntimeError):
    """``run`` was started without an explicit, sufficient call approval."""


def slug(bid: str) -> str:
    return bid.replace("/", "_")


# ------------------------------------------------------------------ plan
PREP_PROMPTS = ("reframe", "doc_classify", "align")
PROMPTS = PREP_PROMPTS + ("doc_claim", "persona")      # normalise: scenario build passes cached values only (0 calls)


def endpoint_description(settings: Mapping) -> str:
    llm = settings["llm"]
    prov = llm["backends"][llm["backend"]]["provider"]["order"]
    return f"{llm['backend']} (OpenRouter) -> {' / '.join(prov)} -> {llm['model']}"


def count_calls(prep, settings: Mapping, personas_probe_seed: int = 1) -> dict:
    """Logical LLM calls of one bid, by prompt, from the real runner code (``call_plan.collect_bid_calls``)."""
    from gtgen.data.call_plan import collect_bid_calls
    coll = collect_bid_calls(prep, settings, fresh=True, slots=[(personas_probe_seed, 0)], n_scenarios=1)
    n = Counter(r["prompt"] for r in coll["calls"])
    return {"reframe": n["reframe"], "doc_classify": n["doc_classify"], "align": n["align"],
            "align_upper": len(coll["align_upper"]), "doc_claim": n["doc_claim"], "persona_per_seed": n["persona"]}


def _max_regen(settings: Mapping) -> int:
    """D060 ``persona.diversity.max_company_regenerations`` (0 when absent): each seed may need this many EXTRA persona
    calls (``persona_unique``); they are real calls only when a company is a near-duplicate."""
    return int(((settings.get("persona", {}) or {}).get("diversity", {}) or {}).get("max_company_regenerations", 0))


def plan_row(bid: str, prep, settings: Mapping, personas: int, fresh: bool = False,
             coll: Optional[Mapping] = None) -> dict:
    """One bid's call counts. ``coll`` = ``call_plan.collect_bid_calls`` result (the real planners run, token-aware, D062);
    without it (settings too small to run the runners: unit tests) the doc_claim count is the fixed-size formula."""
    cons = merged_constraints(prep, settings)
    pairs, reqs = claim_requests(cons, prep.catalogue, settings)
    n_doc = len(_doc_constraints(cons))
    batch = int(settings["doc_claims"]["batch_size"])
    row = {"bid": bid, "n_candidates": len(prep.catalogue.candidates), "doc_proof_active": n_doc,
           "merged_groups": sum(1 for c in cons if c.merged_from), "claim_pairs": len(pairs),
           "claim_requests": len(reqs), "claim_calls": math.ceil(len(reqs) / batch) if reqs else 0,
           "persona_calls": int(personas), "prep_qa_flags": list(prep.qa_flags),
           "persona_calls_upper": int(personas) * (1 + _max_regen(settings)),
           "reframe_calls": 0, "doc_classify_calls": 0, "align_calls": 0, "align_calls_upper": 0,
           "persona_calls_per_seed": 1}
    if coll is None:
        if fresh:
            raise ValueError("a --fresh plan needs the complete settings (llm token budget, persona, relaxation, ...)")
        return row
    n = Counter(r["prompt"] for r in coll["calls"])
    per_seed = max(1, n["persona"] // max(1, int(personas)))
    row.update(reframe_calls=n["reframe"], doc_classify_calls=n["doc_classify"], align_calls=n["align"],
               align_calls_upper=len(coll["align_upper"]) if fresh else 0, claim_calls=n["doc_claim"],
               persona_calls_per_seed=per_seed, persona_calls=n["persona"],
               persona_calls_upper=n["persona"] * (1 + _max_regen(settings)))
    return row


def make_plan(bids: Sequence[str], personas: int, settings: Mapping, prepare: Callable, fresh: bool = False,
              dialogue: bool = True) -> dict:
    """``prepare(bid)`` -> PreparedBid (cache-backed). No LLM call is made here.

    ``fresh=False``: reframe / doc_classify / align are assumed to be cache hits (0 calls), as before.
    ``fresh=True`` (the ``--fresh`` run, which reads no cache, D054): they are real calls. D062: every count comes from
    running the real planning code (token-aware batches, persona v4 prompt rendered with strata and the growing avoid
    list) against a recording client; ``token_report`` has tokens per prompt, ``dialogue`` / ``stage1`` the dialogue
    calls (NOT part of ``estimate_calls``, which stays the scenario-table figure that ``check_approval`` compares).
    Logical calls (``estimate_calls``) are first attempts; HTTP requests add schema/check retries
    (``worst_case_calls``) and transport retries (not bounded here).
    """
    rows = []
    colls: dict[str, dict] = {}
    preps: dict[str, Any] = {}
    tok = can_plan(settings)
    for bi, b in enumerate(bids):
        prep = prepare(b)
        preps[b] = prep
        coll = None
        if tok:
            slots = [(sd, bi * int(personas) + sd - 1) for sd in range(1, int(personas) + 1)]
            coll = collect_bid_calls(prep, settings, fresh=fresh, slots=slots, n_scenarios=len(bids) * int(personas))
            colls[b] = coll
        rows.append(plan_row(b, prep, settings, personas, fresh, coll))
    for r in rows:
        r["total_calls"] = (r["reframe_calls"] + r["doc_classify_calls"] + r["align_calls"] + r["claim_calls"]
                            + r["persona_calls"])
        r["total_calls_upper"] = (r["total_calls"] - r["align_calls"] + (r["align_calls_upper"] if fresh else 0)
                                  + r["persona_calls_upper"] - r["persona_calls"])      # + D060 regenerations
    est = sum(r["total_calls"] for r in rows)
    upper = sum(r["total_calls_upper"] for r in rows)
    from gtgen.data.call_plan import http_factors, worst_minutes
    retries = int(settings["llm"]["max_retries"])
    hf = http_factors(settings)
    pace = float(settings["llm"]["rate_limit"]["min_interval_s"])
    http_attempts = int(settings["llm"].get("http_retry", {}).get("max_attempts", 0))
    per_prompt = {"reframe": sum(r["reframe_calls"] for r in rows), "doc_classify": sum(r["doc_classify_calls"] for r in rows),
                  "align": sum(r["align_calls"] for r in rows), "doc_claim": sum(r["claim_calls"] for r in rows),
                  "persona": sum(r["persona_calls"] for r in rows), "normalise": 0}
    token_report = table_report(colls, settings, fresh, int(personas)) if tok else None
    dlg = None
    stage1 = None
    if tok and dialogue and "dialogue" in settings and "negotiation" in settings and "scenario" in settings:
        dlg = {b: dialogue_report(preps[b], settings, list(range(1, int(personas) + 1))) for b in bids}
        stage1 = stage1_totals(token_report, dlg, settings)
    return {
        "token_report": token_report, "dialogue": dlg, "stage1": stage1,
        "settings_view": {"llm": {k: settings["llm"][k] for k in ("max_tokens_per_call", "min_completion_tokens")}}
        if tok else None,
        "bids": list(bids), "personas": int(personas), "fresh": bool(fresh),
        "doc_claims_batch_size": int(settings["doc_claims"]["batch_size"]),
        "batch_max_constraints": int(settings["llm"].get("batch_max_constraints", 0)),
        "batch_completion_target": int(settings["llm"].get("batch_completion_target", 0)),
        "per_bid": rows, "per_prompt": per_prompt, "max_company_regenerations": _max_regen(settings),
        "estimate_calls": est, "upper_calls": upper,
        "max_retries": retries, "http_max_attempts": http_attempts,
        "http_factors": hf,
        "worst_case_calls": upper * hf["per_call"],      # upper x V x H (V = 1 + max_retries, H = 1 + http_retry.max_attempts)
        "ceiling_requests": upper * hf["ceiling_per_call"],
        "pacing_s": pace,
        "estimate_minutes": est * pace / 60.0, "worst_case_minutes": worst_minutes(upper, hf),
        "ceiling_minutes": (upper * hf["ceiling_per_call"] * pace + upper * hf["V"] ** 2 * hf["backoff_s"]) / 60.0,
        "endpoint": endpoint_description(settings),
        "cached_steps": ("none (fresh: nothing is read from the disk cache)" if fresh else
                         "reframe, doc_classify, align (assumed cache hits; verified by running prepare_bid in mock mode)"),
        "live_requests_made_by_plan": 0,
    }


def render_plan(plan: Mapping) -> str:
    fresh = bool(plan.get("fresh"))
    out = [f"Mode: {'FRESH (no cache reads; every step is a live call)' if fresh else 'cache-assumed (reframe/doc_classify/align = 0)'}"
           f"; persona seeds per bid: {plan['personas']}", ""]
    out += ["| bid | candidates | doc-proof active | claim pairs | requests after prefilter | reframe | doc_classify | align "
            "(max) | doc_claim | persona (seeds x per-seed) | normalise | total calls |", "|" + "---|" * 12]
    for r in plan["per_bid"]:
        al = f"{r['align_calls']} ({r['align_calls_upper']})" if fresh else "0"
        out.append(f"| {r['bid']}{' [prep_not_cached: upper bound]' if PREP_NOT_CACHED in r.get('prep_qa_flags', []) else ''} | {r['n_candidates']} | {r['doc_proof_active']} | {r['claim_pairs']} | "
                   f"{r['claim_requests']} | {r['reframe_calls']} | {r['doc_classify_calls']} | {al} | "
                   f"{r['claim_calls']} | {r['persona_calls']} ({plan['personas']} x {r['persona_calls_per_seed']}) | 0 | "
                   f"{r['total_calls']} |")
    pp = plan["per_prompt"]
    out.append(f"| TOTAL | | | {sum(r['claim_pairs'] for r in plan['per_bid'])} | "
               f"{sum(r['claim_requests'] for r in plan['per_bid'])} | {pp['reframe']} | {pp['doc_classify']} | "
               f"{pp['align']} | {pp['doc_claim']} | {pp['persona']} | 0 | {plan['estimate_calls']} |")
    out += ["",
            f"Per seed (persona call only; doc claims are fetched once per bid and shared by its seeds): "
            + ", ".join(f"{r['bid']}: {r['persona_calls_per_seed']}" for r in plan["per_bid"]) + ".",
            f"Batching: token-aware (D062/D064): output per batched call packed up to batch_completion_target="
            f"{plan.get('batch_completion_target')} tokens, at most batch_max_constraints={plan['batch_max_constraints']} items per "
            f"call, doc claims at most batch_size={plan['doc_claims_batch_size']} (counts come from the real planning code, "
            f"not a formula).",
            f"LOGICAL calls (first attempts): {plan['estimate_calls']} (~{plan['estimate_minutes']:.1f} min at "
            f"{plan['pacing_s']} s pacing, excluding model latency)."]
    if fresh:
        out.append(f"Upper bound if the fresh reframe drops nothing (more align batches): {plan['upper_calls']} calls.")
    if plan.get("max_company_regenerations"):
        out.append(f"D060 company-uniqueness regenerations: up to {plan['max_company_regenerations']} EXTRA persona calls "
                   f"per seed (only when a company name / industry-city repeats); included in the upper bound and the "
                   f"worst case, not in the logical estimate.")
    hf = plan.get("http_factors") or {"V": 1 + plan["max_retries"], "H": 1 + plan["http_max_attempts"], "backoff_s": 0}
    out += [f"HTTP REQUESTS, worst case = upper_calls x V x H, V = 1 + max_retries = {hf['V']} validation attempts, "
            f"H = 1 + http_retry.max_attempts = {hf['H']} HTTP attempts: {plan['upper_calls']} x {hf['V'] * hf['H']} = "
            f"{plan['worst_case_calls']} (~{plan['worst_case_minutes']:.1f} min = pacing + {hf['V']} x {hf['backoff_s']:.0f} s "
            f"backoff per call).",
            f"Absolute ceiling if the runner output-check retries also hit client validation retries (V^2 x H = "
            f"{hf['V'] ** 2 * hf['H']} per call): {plan['ceiling_requests']} (~{plan['ceiling_minutes']:.1f} min).",
            f"Endpoint: {plan['endpoint']}."]
    if plan.get("token_report"):
        out += [""] + render_table_report(plan["token_report"])
    if plan.get("dialogue"):
        from gtgen.data.call_plan import render_stage1
        out += render_dialogue(plan["dialogue"], plan["settings_view"]) + render_stage1(plan)
    notes = sorted({f for r in plan["per_bid"] for f in r.get("prep_qa_flags", []) if f.startswith("plan_")})
    if notes:
        out.append(f"Planning notes: {', '.join(notes)} (plan_align_not_cached: the align step missed the mock cache; align "
                   f"calls are counted from the constraints, which do not depend on the alignments).")
    ub = [r["bid"] for r in plan["per_bid"] if PREP_NOT_CACHED in r.get("prep_qa_flags", [])]
    if ub:
        out.append(f"{PREP_NOT_CACHED} (upper bound): {', '.join(ub)} -- the reframe / doc_classify result was not cached under "
                   f"the current batching, so the rows use the ORIGINAL constraints (no drops) and a heuristic doc-proof set "
                   f"(call_plan.heuristic_doc_proof); reframe / doc_classify / align / doc_claim counts are upper bounds.")
    out.append("Live requests made by this command: 0")
    return "\n".join(out)


# ------------------------------------------------------------------ run
def check_approval(plan: Mapping, approved_calls: Optional[int] = None, cap: Optional[int] = None) -> str:
    """INFORMATIONAL only (D066): there is no request cap. Returns a one-paragraph description of the plan's
    logical / upper / worst-case HTTP figures next to any figure the user passed; never raises because of a number."""
    worst = int(plan["worst_case_calls"])
    msg = (f"plan: logical estimate {plan['estimate_calls']}, upper {plan.get('upper_calls', plan['estimate_calls'])}, "
           f"worst-case HTTP requests {worst} (x (1 + max_retries={plan.get('max_retries')}) x "
           f"(1 + http_max_attempts={plan.get('http_max_attempts')})). No request cap is enforced (D066).")
    if approved_calls is not None:
        msg += f" --approved-calls {approved_calls} is informational"
        msg += " (below the worst case)." if approved_calls < worst else "."
    return msg


def render_observed(plan: Mapping, status: Mapping, client: Any = None) -> str:
    """Observed vs planned HTTP requests. Observed = requests that actually passed the HTTPGate (every attempt,
    including schema-validation and transport retries)."""
    line = (f"OBSERVED HTTP requests sent (incl. retries): {status['http_requests']} | planned worst case: "
            f"{plan['worst_case_calls']} | planned logical estimate: {plan['estimate_calls']}")
    summ = getattr(client, "provenance_summary", None)
    if callable(summ):
        p = summ()
        line += (f"\nClient provenance events (logical calls): live={p['live']} cache_hit={p['cache_hit']} "
                 f"fixture={p['fixture']} total={p['total']}")
    return line


def _write_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False, default=str)


def run_table(plan: Mapping, settings: Mapping, runners, gate, out_dir: str, *, approved_calls: Optional[int],
              cap: Optional[int], prepare: Optional[Callable] = None, run_key: Optional[str] = None) -> dict:
    """Build every (bid, seed 1..personas); claims are fetched once per bid and shared by its personas.

    ``gate`` is an HTTPGate (installed on httpx by the CLI). Stops BEFORE sending once the cap is reached or on an
    authentication / payment failure, and always writes ``run_status.json`` (``complete`` false when stopped early).

    ``run_key`` (D060, optional): when given, every scenario of the run is built with the run context
    (n = len(bids) * personas, global scenario index in (bid, seed) order, one shared ``accepted_profiles`` list), i.e.
    stratified personas + company-uniqueness regeneration. Regenerations are EXTRA persona calls, bounded in the plan by
    ``persona.diversity.max_company_regenerations`` per seed (``upper_calls``/``worst_case_calls``). Without it the
    legacy path is used.
    """
    print(check_approval(plan, approved_calls, cap), file=sys.stderr, flush=True)
    if prepare is None:
        from gtgen.cli import prepare_bid
        prepare = lambda b: prepare_bid(b, settings, runners)  # noqa: E731
    os.makedirs(out_dir, exist_ok=True)
    done: list[str] = []
    todo = [(b, s) for b in plan["bids"] for s in range(1, int(plan["personas"]) + 1)]
    stopped: Optional[str] = None
    accepted_profiles: list = []
    n_scen = len(todo)
    for bid in plan["bids"]:
        if stopped:
            break
        try:
            prep = prepare(bid)
            claims = fetch_doc_claims(merged_constraints(prep, settings), prep.catalogue, runners, settings,
                                      gate=gate, scenario_id=f"{prep.scenario_id_base}_claims")
            for seed in range(1, int(plan["personas"]) + 1):
                ctx_kw = {} if run_key is None else dict(
                    run_key=run_key, n_scenarios=n_scen, scenario_index=todo.index((bid, seed)),
                    accepted_profiles=accepted_profiles)
                b = build_scenario(bid, settings, runners, persona_seed=seed, doc_claims=claims, prepared=prep,
                                   **ctx_kw)
                name = f"{slug(bid)}_s{seed}"
                _write_json(os.path.join(out_dir, name + ".json"), scenario_summary(b))
                done.append(name)
        except Exception as exc:
            if type(exc).__name__ != "FatalHTTP":
                stopped = f"error {type(exc).__name__}: {str(exc)[:300]}"
            else:
                stopped = f"{type(exc).__name__}: {exc}"
    unprocessed = [f"{slug(b)}_s{s}" for b, s in todo if f"{slug(b)}_s{s}" not in done]
    status = {"complete": stopped is None and not unprocessed, "incomplete": stopped is not None or bool(unprocessed),
              "stopped": stopped, "http_requests": gate.count, "cap": cap, "approved_calls": approved_calls,
              "plan_estimate_calls": plan["estimate_calls"], "done": done, "unprocessed": unprocessed}
    _write_json(os.path.join(out_dir, "run_status.json"), status)
    return status


# ------------------------------------------------------------------ report (section 9)
def load_results(in_dir: str) -> tuple[list[dict], Optional[dict]]:
    res = []
    for fn in sorted(os.listdir(in_dir)):
        if fn.endswith(".json") and fn not in ("plan.json", "run_status.json"):
            with open(os.path.join(in_dir, fn), encoding="utf-8") as f:
                res.append(json.load(f))
    st = None
    sp = os.path.join(in_dir, "run_status.json")
    if os.path.isfile(sp):
        with open(sp, encoding="utf-8") as f:
            st = json.load(f)
    return res, st


def _mmm(xs: Sequence[float], digits: int = 1) -> str:
    if not xs:
        return "n/a"
    m = statistics.fmean(xs)
    f = (lambda v: f"{v:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{v:.0f}")
    return f(m) if min(xs) == max(xs) else f"{f(m)} ({f(min(xs))}-{f(max(xs))})"


def render_section9(results: Sequence[Mapping], status: Optional[Mapping] = None) -> str:
    bids = sorted({r["bid"] for r in results})
    out = [SECTION_HEADER, "",
           "Each scenario is one (bid, persona seed): the reframed and merged (D046) bid constraints get their role "
           "from evidence coverage (D048), document claims come from quote-verified extraction (D051), the persona LLM "
           "picks the synthetic commercial terms inside code-derived bands, and the code tightens them "
           "(D049) until the initial VERIFIED feasible count is below `negotiation.min_shortlist`. "
           "Cells are `mean (min-max)` over the persona seeds that built (`ok`). "
           "*Initial verified-feasible* = candidates with no scored HARD FAIL/UNKNOWN and no scored SOFT FAIL. "
           "*soft_only_blocked* = candidates with no scored HARD FAIL/UNKNOWN and at least one scored SOFT FAIL. "
           "*Triggers* = scenarios with initial verified-feasible < min_shortlist AND soft_only_blocked >= 1.", ""]
    if status and status.get("incomplete"):
        out += [f"**INCOMPLETE RUN**: stopped ({status.get('stopped')}); {len(status.get('done', []))} scenario(s) "
                f"done, {len(status.get('unprocessed', []))} not processed; HTTP requests {status.get('http_requests')} "
                f"of cap {status.get('cap')}.", ""]
    if not results:
        out += ["No scenario results found.", ""]
    else:
        out += ["| bid | ok/seeds | neg. HARD | neg. SOFT (incl. synthetic) | verification_only (HARD / SOFT) | dropped | merged groups "
                "| tightness target | verified-feasible BEFORE | verified-feasible AFTER | soft_only_blocked | triggers "
                "| claim requests | claims found | prefiltered | failed |",
                "|" + "---|" * 16]
    per_bid: dict[str, list[dict]] = {}
    for b in bids:
        rows = [r for r in results if r["bid"] == b]
        ok = [r for r in rows if r.get("ok")]
        per_bid[b] = ok
        if not ok:
            out.append(f"| {b} | 0/{len(rows)} |" + " n/a |" * 14)
            continue
        c = lambda k: [r["counts"][k] for r in ok if k in r["counts"]]  # noqa: E731
        cl = lambda k: [r["claims"].get(k, 0) for r in ok]  # noqa: E731
        tg = sorted({r["tightness"].get("target") for r in ok})
        trig = sum(1 for r in ok if r["negotiation_triggers_naturally"])
        out.append(
            f"| {b} | {len(ok)}/{len(rows)} | {_mmm(c('negotiated_hard'))} | {_mmm(c('negotiated_soft'))} | "
            f"{_mmm(c('verification_only'))} ({_mmm(c('verification_only_hard'))} / "
            f"{_mmm(c('verification_only_soft'))}) | {_mmm(c('dropped'))} | {_mmm(c('merged_groups'))} | "
            f"{tg[0] if len(tg) == 1 else f'{tg[0]}-{tg[-1]}'} | "
            f"{_mmm([r['initial_verified_feasible_before'] for r in ok])} | "
            f"{_mmm([r['initial_verified_feasible_after'] for r in ok])} | "
            f"{_mmm([r['soft_only_blocked'] for r in ok])} | {trig}/{len(ok)} | {_mmm(cl('n_requests'))} | "
            f"{_mmm(cl('n_claims'))} | {_mmm(cl('n_prefiltered'))} | {_mmm(cl('n_failed'))} |")
    out.append("")
    if per_bid:
        out += ["### Synthetic commercial terms per bid", "", "| bid | present | omitted (reason) | tightening steps |",
                "|---|---|---|---|"]
        for b, ok in per_bid.items():
            if not ok:
                out.append(f"| {b} | n/a | n/a | n/a |")
                continue
            r0 = ok[0]
            om = "; ".join(f"{t} ({w})" for t, w in sorted(r0["omitted_terms"].items())) or "none"
            out.append(f"| {b} | {', '.join(r0['bands_terms']) or 'none'} | {om} | "
                       f"{_mmm([r['tightness'].get('steps', 0) for r in ok])} |")
        out.append("")
    nat_all = [b for b, ok in per_bid.items() if ok and all(r["negotiation_triggers_naturally"] for r in ok)]
    nat_some = [b for b, ok in per_bid.items() if ok and any(r["negotiation_triggers_naturally"] for r in ok)
                and b not in nat_all]
    nat_none = [b for b, ok in per_bid.items() if ok and not any(r["negotiation_triggers_naturally"] for r in ok)]
    nok = [b for b, ok in per_bid.items() if ok]
    n_scen = sum(len(ok) for ok in per_bid.values())
    n_trig = sum(1 for ok in per_bid.values() for r in ok if r["negotiation_triggers_naturally"])
    out += ["### Reading", ""]
    if nok:
        out += [f"- A negotiation starts naturally (initial verified-feasible < min_shortlist and at least one "
                f"candidate blocked only by SOFT constraints) in all persona seeds of {len(nat_all)} of {len(nok)} "
                f"bids ({', '.join(nat_all) or 'none'}), in some seeds of {len(nat_some)} "
                f"({', '.join(nat_some) or 'none'}), and in none for {len(nat_none)} "
                f"({', '.join(nat_none) or 'none'}).",
                f"- Over all scenarios: {n_trig} of {n_scen} trigger a negotiation naturally.",
                "- A bid where verified-feasible is already below min_shortlist but soft_only_blocked is 0 has no SOFT "
                "lever: the dialogue could only end in BEST_AVAILABLE or concede a HARD constraint (which is never "
                "relaxed)."]
    else:
        out += ["- No scenario built successfully; nothing to read."]
    out += [""] + render_blockers(per_bid) + render_calibration(per_bid)
    out += ["### Per-tier perfect-deal share from the dialogue: PENDING", "",
            "PENDING (needs the dialogue run). Calibration above is the deterministic simulation only.", ""]
    return "\n".join(out)


BLOCKER_COLS = ("real_soft_blocked", "synthetic_commercial_blocked", "observed_fail_blocked", "mixed_blocked")


def _blocker_row(label: str, rows: Sequence[Mapping]) -> str:
    with_b = [r for r in rows if r.get("blockers")]
    if not with_b:
        return f"| {label} | {len(rows)} |" + " n/a |" * 6
    tot = [sum(r["blockers"].get(k, 0) for r in with_b) for k in BLOCKER_COLS]
    allb = sum(r["soft_only_blocked"] for r in with_b)
    return (f"| {label} | {len(with_b)} | " + " | ".join(str(x) for x in tot)
            + f" | {sum(tot)} | {allb} | {_mmm([r['soft_only_blocked'] for r in with_b])} |")


def render_blockers(per_bid: Mapping[str, Sequence[Mapping]]) -> list[str]:
    """Soft-only blockers split by cause (measured; no tuning), per bid and per persona tier.  Counts are candidate
    totals summed over the scenarios of the row; the four causes are exclusive and sum to soft_only_blocked."""
    allr = [r for ok in per_bid.values() for r in ok]
    hdr = ["| {} | scenarios | real GeM-derived SOFT | synthetic_commercial | observed FAIL on verification_only "
           "| mixed (>1 cause) | sum | soft_only_blocked | per scenario mean (min-max) |", "|---|---|---|---|---|---|---|---|---|"]
    out = ["### Soft-only blockers by cause", "",
           "A blocker is a candidate with no scored HARD FAIL/UNKNOWN and at least one scored SOFT FAIL. Cause = what "
           "the failing SOFT constraints are: a real (bid-derived) negotiated SOFT constraint, a synthetic commercial "
           "term, or an OBSERVED FAIL on a verification_only SOFT constraint. A candidate failing several kinds is "
           "counted in `mixed`. Values are summed over the scenarios of the row.", "", "Per bid:", ""]
    out += [hdr[0].format("bid"), hdr[1]]
    out += [_blocker_row(b, ok) for b, ok in per_bid.items() if ok]
    out += ["", "Per tier:", "", hdr[0].format("tier"), hdr[1]]
    for tier in ("lenient", "medium", "medium_strict", "strict"):
        rows = [r for r in allr if r.get("tier") == tier]
        if rows:
            out.append(_blocker_row(tier, rows))
    out.append("")
    return out


def render_calibration(per_bid: Mapping[str, Sequence[Mapping]]) -> list[str]:
    """Deterministic calibration outcome per tier (D050): share of scenarios whose chosen tightness candidate puts
    top1_match_original inside the tier band, and the distribution of the chosen value."""
    allr = [r for ok in per_bid.values() for r in ok if r.get("calibration")]
    out = ["### Calibration per tier (simulated, no LLM)", ""]
    if not allr:
        return out + ["No calibration records (calibration skipped or not run).", ""]
    out += ["| tier | band | scenarios | in band | uncalibrated | chosen top1_match_original mean (min-max) "
            "| candidates tried mean (min-max) |", "|---|---|---|---|---|---|---|"]
    for tier in ("lenient", "medium", "medium_strict", "strict"):
        rows = [r for r in allr if r["calibration"].get("tier") == tier]
        if not rows:
            continue
        band = rows[0]["calibration"]["band"]
        vals = [r["calibration"]["chosen_top1_match_original"] for r in rows
                if r["calibration"].get("chosen_top1_match_original") is not None]
        n_in = sum(1 for r in rows if r["calibration"].get("in_band"))
        out.append(f"| {tier} | {band[0]}-{band[1]} | {len(rows)} | {n_in}/{len(rows)} | {len(rows) - n_in} | "
                   f"{_mmm(vals, 2)} | {_mmm([r['calibration'].get('n_candidates', 0) for r in rows])} |")
    out.append("")
    return out


def write_section(path: str, section: str) -> None:
    """Replace (or append) section 9 of the markdown file; sections 1-8 are preserved verbatim."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    marker = "\n" + SECTION_HEADER
    i = text.find(marker)
    head = text[:i] if i >= 0 else text.rstrip("\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(head.rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n")


# ------------------------------------------------------------------ CLI
def resolve_bid(raw: str) -> str:
    """Full id, or a bare number matched against DEFAULT_BIDS."""
    if "/" in raw:
        return raw
    hits = [b for b in DEFAULT_BIDS if b.endswith("/" + raw)]
    if len(hits) != 1:
        raise SystemExit(f"cannot resolve bid {raw!r}; give the full id")
    return hits[0]


def check_fresh_agreement(plan_fresh: bool, client_fresh: bool) -> None:
    """The plan and the executing client must agree on fresh, otherwise the estimate is wrong."""
    if bool(plan_fresh) != bool(client_fresh):
        raise ApprovalError(f"plan.json fresh={bool(plan_fresh)} but the LLM client fresh={bool(client_fresh)}; "
                            f"re-plan with{'' if client_fresh else 'out'} --fresh or fix GTGEN_LLM_FRESH / --fresh")


PREP_NOT_CACHED = "plan_prep_not_cached"
PREP_LEGACY_CACHE = "plan_prep_legacy_batching_cache"   # served from an entry cached under the pre-D064 batch settings
# Settings of the earlier batching regimes (5e01119 / D062: 15k budget; pre-D062: 10k budget). A reframe / doc_classify entry
# cached under one of them is still a valid answer for the SAME constraints, so the planner tries them before giving up.
LEGACY_LLM_VARIANTS = ({"max_tokens_per_call": 15000, "max_completion_tokens": 6000, "batch_completion_target": 6000,
                        "batch_max_constraints": 25},
                       {"max_tokens_per_call": 10000, "max_completion_tokens": 6000, "batch_completion_target": 6000,
                        "batch_max_constraints": 25})


def tolerant_runners(client, settings):
    """Planning-only runners (no live call; the client is the mock/cache client).

    * align miss: not fatal (alignments change no call count) -> qa_flag ``plan_align_not_cached``.
    * reframe / doc_classify miss (D064: bigger batches changed the cache keys): NOT fatal either.  The runner first retries
      the SAME call under the earlier batch settings (``LEGACY_LLM_VARIANTS``: entries cached for the same constraints).
      If none hits, the step returns the ORIGINAL constraints (reframe: nothing dropped, so the plan is an upper bound on
      the active constraints; doc_classify: ``call_plan.heuristic_doc_proof`` marks every constraint whose text suggests a
      document, an upper bound on the doc-proof set) with the qa_flag ``plan_prep_not_cached`` -> the row is marked
      ``plan_prep_not_cached (upper bound)``.
    """
    from gtgen.data.call_plan import heuristic_doc_proof
    from gtgen.llm import LLMResult, LLMRunners

    class _PlanRunners(LLMRunners):
        def align(self, *a, **k):
            try:
                return super().align(*a, **k)
            except FileNotFoundError:
                return LLMResult([], [], ["plan_align_not_cached"])

        def _legacy(self, name, *a, **k):
            for var in LEGACY_LLM_VARIANTS:
                st = dict(self.settings)
                st["llm"] = {**self.settings["llm"], **var}
                try:
                    res = getattr(LLMRunners(self.client, st), name)(*a, **k)
                    res.qa_flags.append(PREP_LEGACY_CACHE)          # note only: counts are exact for this step
                    return res
                except FileNotFoundError:
                    continue
            return None

        def reframe(self, product_category, constraints, *a, **k):
            try:
                return super().reframe(product_category, constraints, *a, **k)
            except FileNotFoundError:
                hit = self._legacy("reframe", product_category, constraints, *a, **k)
                return hit if hit is not None else LLMResult(list(constraints), [], [PREP_NOT_CACHED])

        def doc_classify(self, constraints, *a, **k):
            try:
                return super().doc_classify(constraints, *a, **k)
            except FileNotFoundError:
                hit = self._legacy("doc_classify", constraints, *a, **k)
                if hit is not None:
                    return hit
                return LLMResult(heuristic_doc_proof(constraints), [], [PREP_NOT_CACHED])
    return _PlanRunners(client, settings)


def with_flag(settings: Mapping, flag: bool) -> dict:
    return {**settings, "llm": {**settings["llm"], "deterministic_first": bool(flag)}}


def resolve_prep_once(bid: str, settings: Mapping, prepare_with: Callable) -> tuple[Any, str]:
    """Prep (reframe + doc_classify -> doc-proof set) resolved ONCE per bid for a rules comparison.  The cache key depends on
    ``llm.deterministic_first``, so try both flags; the first prep without ``plan_prep_not_cached`` wins ("cache"), else
    the heuristic upper-bound prep ("heuristic").  Both flags then plan from this same prep."""
    first = None
    for flag in (False, True):
        prep = prepare_with(with_flag(settings, flag), bid)
        if PREP_NOT_CACHED not in prep.qa_flags:
            return prep, "cache"
        first = first or prep
    return first, "heuristic"


def compare_rules(bids: Sequence[str], personas: int, settings: Mapping, prepare_with: Callable, fresh: bool = True) -> dict:
    """Plan with ``deterministic_first`` true and false from IDENTICAL prep inputs; only rule_split item counts differ."""
    preps, src = {}, {}
    for b in bids:
        preps[b], src[b] = resolve_prep_once(b, settings, prepare_with)
    plans = {flag: make_plan(list(bids), personas, with_flag(settings, flag), lambda b: preps[b], fresh=fresh, dialogue=False)
             for flag in (True, False)}
    return {"prep_source": src, "plans": plans, "fresh": fresh, "personas": personas}


def render_compare(cmp: Mapping) -> str:
    out = ["Prep source per bid (same prep used for BOTH flags): " + ", ".join(f"{b}={s}" for b, s in cmp["prep_source"].items()),
           "", "| prompt | flag | items | rule | prefilter | LLM items | LLM calls |", "|---|---|---|---|---|---|---|"]
    for p in ("reframe", "doc_classify", "align", "doc_claim", "persona"):
        for flag in (True, False):
            bp = cmp["plans"][flag]["token_report"]["by_prompt"].get(p)
            if bp is None:
                continue
            out.append(f"| {p} | {str(flag).lower()} | {bp['n_items']} | {bp['n_rule']} | {bp['n_prefilter']} | {bp['n_llm']} | {bp['calls']} |")
    for flag in (True, False):
        pl = cmp["plans"][flag]
        out.append(f"deterministic_first={str(flag).lower()}: estimate_calls={pl['estimate_calls']} upper_calls={pl['upper_calls']} "
                   f"worst_case_http={pl['worst_case_calls']}")
    return "\n".join(out)


def cmd_plan(args) -> int:
    if os.environ.get("GTGEN_LLM_MODE", "").lower() != "mock":
        raise SystemExit("plan must run with GTGEN_LLM_MODE=mock (no live calls at planning stage)")
    from gtgen import cli  # noqa: F401  (loads .env; never printed)
    from gtgen.data.loaders import load_settings
    from gtgen.llm import LLMClient, LLMRunners
    settings = load_settings()
    if getattr(args, "compare_rules", False):
        bids = [resolve_bid(b) for b in args.bids.split(",") if b]
        def prepare_with(st_, b):
            return cli.prepare_bid(b, st_, tolerant_runners(LLMClient(st_, fresh=False), st_))
        print(render_compare(compare_rules(bids, args.personas, settings, prepare_with, fresh=args.fresh)))
        return 0
    # prepare_bid is cache-backed on purpose (it only supplies the reframe drops / catalogue); --fresh affects the
    # counting, not this client.
    runners = tolerant_runners(LLMClient(settings, fresh=False), settings)
    bids = [resolve_bid(b) for b in args.bids.split(",") if b]
    plan = make_plan(bids, args.personas, settings, lambda b: cli.prepare_bid(b, settings, runners), fresh=args.fresh)
    os.makedirs(args.out, exist_ok=True)
    _write_json(os.path.join(args.out, "plan.json"), plan)
    print(render_plan(plan))
    return 0


def cmd_run(args) -> int:
    if os.environ.get("GTGEN_LLM_MODE", "").lower() != "live":
        raise SystemExit("run needs GTGEN_LLM_MODE=live (it is the approved live step)")
    from gtgen import cli  # noqa: F401
    from gtgen.data.loaders import load_settings
    from gtgen.llm import LLMClient, LLMRunners
    from gtgen.llm.probe import HTTPGate
    settings = load_settings()
    with open(os.path.join(args.out, "plan.json"), encoding="utf-8") as f:
        plan = json.load(f)
    if args.fresh:
        os.environ["GTGEN_LLM_FRESH"] = "1"
    client = LLMClient(settings, fresh=True if args.fresh else None)
    try:
        print(check_approval(plan, args.approved_calls, args.cap))
        check_fresh_agreement(plan.get("fresh", False), client.fresh)
    except ApprovalError as exc:
        raise SystemExit(f"REFUSED: {exc}")
    gate = HTTPGate()
    gate.install()
    runners = LLMRunners(client, settings)
    st = run_table(plan, settings, runners, gate, args.out, approved_calls=args.approved_calls, cap=args.cap)
    print(json.dumps(st, indent=1))
    print(render_observed(plan, st, client))
    return 0 if st["complete"] else 1


def cmd_report(args) -> int:
    res, st = load_results(args.inp)
    section = render_section9(res, st)
    if args.out_section:
        write_section(args.out, section)
        print(f"wrote section 9 into {args.out}")
    else:
        print(section)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--bids", default=",".join(DEFAULT_BIDS))
    p.add_argument("--personas", type=int, default=4)
    p.add_argument("--out", default=DEFAULT_DIR)
    p.add_argument("--fresh", action="store_true",
                   help="plan a --fresh run: reframe/doc_classify/align are real calls (no cache reads)")
    p.add_argument("--compare-rules", action="store_true",
                   help="print deterministic_first true vs false side by side from IDENTICAL prep inputs")
    r = sub.add_parser("run")
    r.add_argument("--approved-calls", type=int, help="informational only (D066): compared with the plan's worst-case HTTP requests in a printed note; never refuses")
    r.add_argument("--approved-requests", dest="approved_calls", type=int, help=argparse.SUPPRESS)
    r.add_argument("--cap", type=int, default=None, help="ignored (D066: no request cap); accepted for compatibility")
    r.add_argument("--out", default=DEFAULT_DIR, help="directory holding plan.json; results are written here")
    r.add_argument("--fresh", action="store_true", help="read no LLM cache (sets GTGEN_LLM_FRESH=1); must match plan.json")
    t = sub.add_parser("report")
    t.add_argument("--in", dest="inp", default=DEFAULT_DIR)
    t.add_argument("--out", default="docs/coverage.md")
    t.add_argument("--out-section", action="store_true",
                   help="replace/append section 9 inside --out (sections 1-8 untouched); otherwise print to stdout")
    a = ap.parse_args(argv)
    return {"plan": cmd_plan, "run": cmd_run, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
