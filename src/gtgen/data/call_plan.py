"""Token-aware LLM call planning for stage 1 (D062). No network, no cache, no live request.

Two parts:

1. SCENARIO TABLE (``collect_bid_calls``): the real runner code (reframe / doc_classify / align / doc_claim / persona v7) is
   executed against a ``RecordingClient`` that records, for every call it would make, the estimated input tokens of the
   REAL rendered prompt (client estimator) and the batch plan (``batch_info``) and then answers with a validation error so
   the runner carries on with the next batch.  Counts therefore come from the code that will run, not from a formula.

2. DIALOGUE (``dialogue_call_range`` / ``dialogue_prompt_tokens``): the number of LLM calls of one negotiation as a
   closed form of the brief, derived from ``dialogue/loop.py``, ``negotiator.py`` and ``buyer.py`` (see ``DialogueCounts``),
   and token estimates per dialogue prompt obtained by rendering the real prompt templates (through the real runner
   methods) with payloads sized from the bid's real catalogue and constraints.

OWNER: data-steward.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

PREP_PROMPTS = ("reframe", "doc_classify", "align")
TABLE_PROMPTS = PREP_PROMPTS + ("doc_claim", "persona")
DIALOGUE_PROMPTS = ("buyer_opening", "reveal_check", "negotiator_extract", "negotiator_interpret", "negotiator_reply",
                    "buyer_reply", "negotiator_acknowledge", "negotiator_clarify", "buyer_reveal", "doc_claim",
                    "negotiator_final")

_NEEDED_KEYS = (("llm", "max_tokens_per_call"), ("llm", "max_completion_tokens"), ("llm", "min_completion_tokens"),
                ("llm", "batch_max_constraints"), ("llm", "batch_completion_target"), ("llm", "max_retries"), ("ranking", "default_weights"),
                ("doc_claims", "batch_size"), ("persona", "diversity"), ("relaxation", "persona_ranges"))


def http_factors(settings: Mapping) -> dict:
    """Worst-case request arithmetic (D063/D064), stated once.

    One LOGICAL call = up to ``V = 1 + llm.max_retries`` client attempts (schema-validation retries; 7 with max_retries=6,
    D066); each attempt is up to ``H = 1 + llm.http_retry.max_attempts`` HTTP requests (first try + transport retries on
    429 / 5xx / timeout, 7 with 6 retries).  So  worst HTTP requests per logical call = V * H  (= 49).  The runner's own
    output-check retries can multiply again (ceiling V * V * H).  INFORMATIONAL ONLY: there is no request cap (D066).  Backoff: the transport retries of one failing attempt wait
    ``sum(http_retry.backoff_s)`` seconds (8+16+...+48 = 168 s).
    """
    llm = settings["llm"]
    hr = llm.get("http_retry", {}) or {}
    v = 1 + int(llm["max_retries"])
    h = 1 + int(hr.get("max_attempts", 0))
    backoff = float(sum((hr.get("backoff_s") or [])[:int(hr.get("max_attempts", 0))]))
    return {"V": v, "H": h, "per_call": v * h, "ceiling_per_call": v * v * h, "backoff_s": backoff,
            "pace_s": float(llm["rate_limit"]["min_interval_s"])}


def worst_minutes(upper_calls: int, f: Mapping) -> float:
    """Wall clock of the worst case: every HTTP request paced at ``pace_s`` plus one full backoff chain per failing attempt
    (V attempts per call)."""
    return (upper_calls * f["per_call"] * f["pace_s"] + upper_calls * f["V"] * f["backoff_s"]) / 60.0


# Words that suggest a constraint is satisfied by a document (used ONLY for the plan's upper bound when the cached
# doc_classify result is unavailable; never for scenario building).
_DOC_HINTS = ("certif", "licen", "test report", "testreport", "iso ", "iso:", "isi ", "isi mark", "bis ", "conformity",
              "accredit", "nabl", "ilac", "authoris", "authoriz", "registration", "datasheet", "data sheet", "brochure",
              "warranty card", "approval", "declaration", "msds", "udyog", "gst", "pan card", "balance sheet", "turnover",
              "experience", "document", "affidavit", "undertaking", "inspection report", "calibration")


def heuristic_doc_proof(constraints: Sequence) -> list:
    """Upper-bound doc-proof set: every constraint whose attribute / spec name / raw text / category contains a document
    word (``_DOC_HINTS``) or whose category is CERTIFICATION gets ``is_doc_proof = True`` with ``doc_name`` = its attribute
    text.  Over-counts by design (more doc-proof constraints = more claim requests = a larger plan)."""
    out = []
    for c in constraints:
        text = " ".join(str(x) for x in (c.attribute, c.spec_name, c.raw_text, c.constraint_category) if x).lower() + " "
        if str(c.constraint_category or "").upper() == "CERTIFICATION" or any(h in text for h in _DOC_HINTS):
            c = c.model_copy(update={"is_doc_proof": True, "doc_name": (c.spec_name or c.attribute)[:60]})
        out.append(c)
    return out


def can_plan(settings: Mapping) -> bool:
    """True when ``settings`` is complete enough to run the real runners (the shipped settings are)."""
    try:
        return all(k in settings[a] for a, k in _NEEDED_KEYS)
    except (KeyError, TypeError):
        return False


# ------------------------------------------------------------------ recording client
class CountingClient:
    """Stand-in LLM client for planning: records one entry per ``call`` and answers with ``LLMValidationError``.

    The real runner turns that into ``ScenarioSkipped`` (D066); ``_runners`` converts it back into flag-and-continue, so
    running the real runner code against this client enumerates exactly the first-attempt calls of a live run (the
    NEXT batch is still measured).  Each entry keeps the
    prompt name, the estimated input tokens of the real rendered request (messages + schema, as the real client
    estimates it) and the runner's ``batch_info`` (batch size and planned tokens) when it batches.  It never touches the
    network or the cache.
    """
    strict = False

    def __init__(self) -> None:
        self.calls: Counter = Counter()
        self.records: list[dict] = []

    def call(self, prompt_name, prompt_version, messages, response_model, **kw):
        from gtgen.llm.client import estimate_messages_tokens, schema_text
        from gtgen.llm.exceptions import LLMValidationError
        self.calls[prompt_name] += 1
        self.records.append({"prompt": prompt_name, "version": prompt_version,
                             "est_in": estimate_messages_tokens(messages, schema_text(response_model)),
                             "batch": kw.get("batch_info")})
        raise LLMValidationError(prompt_name, prompt_version, [], "planner dry-run")


RecordingClient = CountingClient


def _runners(settings: Mapping):
    """Real ``LLMRunners`` over a ``CountingClient``.  D066: ``_call`` now RAISES ``ScenarioSkipped`` when a call fails;
    while measuring, that is the old flag-and-continue (the dry-run client always fails), so the NEXT batch is still
    enumerated.  Only this planner subclass swallows it; the production runner is untouched."""
    from gtgen.llm import LLMRunners, ScenarioSkipped

    class _MeasuringRunners(LLMRunners):
        def _call(self, *a, **kw):
            try:
                return super()._call(*a, **kw)
            except ScenarioSkipped as exc:
                return None, [], str(exc)

    c = CountingClient()
    return c, _MeasuringRunners(c, dict(settings))


# ------------------------------------------------------------------ persona (prompt v7) estimate
def persona_completion_tokens(n_categories: int) -> int:
    # n_categories already includes LOCATION (D076, persona v7) when the caller used with_location
    """Expected reply size of persona v7: a representative filled-in answer (text lengths as the prompt asks for)."""
    from gtgen.llm.client import estimate_tokens
    cats = [f"CATEGORY_{i}" for i in range(max(1, n_categories))]
    sample = {
        "company_name": "Sri Lakshmi Precision Industries Pvt Ltd", "industry": "Industrial fabrication", "city": "Coimbatore",
        "style": "Speaks formally and tersely, wants numbers first, dislikes small talk and repeats a requirement when it is skipped.",
        "tau": 0.613, "category_tolerance": [{"category": c, "multiplier": 0.8} for c in cats], "counter_band": 0.15,
        "patience_turns": 8, "max_concessions_per_turn": 2, "max_relaxations": 2, "patience_rounds": 3,
        "decision_randomness": 0.27, "tolerance": [{"category": c, "value": 2.5} for c in cats],
        "voice": {"formality": "formal", "verbosity": "terse", "regional_voice": "Tamil-English business register"},
        "sales_persona": {"name": "Karthik", "speaking_style": "Warm and unhurried, uses the buyer's own words.",
                          "sales_approach": "Eager to close but only states facts the listing backs up."},
        "ranking_weights": {k: 0.14 for k in ("spec_match", "price_score", "moq_fit", "rating_bayes", "tenure_years",
                                              "location_match", "evidence_confidence")},
        "commercial_terms": {"budget_inr": 1250000.0, "qty_vs_moq": 0.25,
                             "preferred_states": ["Tamil Nadu", "Karnataka", "Kerala"]},
    }
    return estimate_tokens(json.dumps(sample, ensure_ascii=False, separators=(",", ":")))


def _persona_records(prep, settings: Mapping, slots: Sequence[tuple[int, int]], n_scenarios: int, run_key: str
                     ) -> list[dict]:
    """One persona call per (seed, global scenario index); the avoid list holds the ``index`` companies created before
    (the run's accepted profiles), each about as long as a real profile, so the prompt grows with the run."""
    from gtgen.data.scenario import commercial_bands, tightness_target
    from gtgen.data.scenario_build import _INACTIVE, merged_constraints
    from gtgen.llm.strata import persona_strata, with_location
    quantity = float(prep.brief.quantity)
    bands = commercial_bands(prep.catalogue, quantity, settings)
    sc = settings["scenario"]
    cons = merged_constraints(prep, settings)
    cats = [c.constraint_category for c in cons if c.framing_decision not in _INACTIVE and c.constraint_category] + \
           [sc["commercial_terms"]["category"][t] for t in bands["terms"]]
    cats = with_location(cats, settings)      # D076: LOCATION persona tolerance category (persona prompt v7)
    out = []
    for seed, index in slots:
        avoid = [{"company_name": f"Shree Ganesh Engineering Works {i}", "industry": "Industrial machinery",
                  "city": "Coimbatore"} for i in range(index)]
        c, r = _runners(settings)
        strata = persona_strata(seed, n_scenarios, run_key, settings, cats, index=index)
        r.persona(prep.product_category, quantity, prep.brief.quantity_unit, None, cats, seed, f"{prep.bid_id}:s{seed}",
                  bands=bands, tightness_target=tightness_target(seed, settings), strata=strata, avoid_profiles=avoid)
        rec = c.records[0]
        out.append({"prompt": "persona", "est_in": rec["est_in"], "est_out": persona_completion_tokens(len(set(cats))),
                    "size": 1, "avoid": len(avoid), "version": rec["version"]})
    return out


# ------------------------------------------------------------------ scenario table: records per bid
def _batched(c: CountingClient, settings: Mapping) -> list[dict]:
    out = []
    mc = int(settings["llm"]["min_completion_tokens"])
    for rec in c.records:
        b = rec["batch"] or {}
        est_out = int(b.get("est_completion_tokens", mc))
        out.append({"prompt": rec["prompt"], "est_in": rec["est_in"], "est_out": est_out,
                    "size": b.get("batch_size"), "planned_in": b.get("est_input_tokens"), "version": rec["version"]})
    return out


def collect_bid_calls(prep, settings: Mapping, *, fresh: bool, slots: Sequence[tuple[int, int]],
                      n_scenarios: int, run_key: str = "plan") -> dict:
    """Records of every table-stage LLM call of one bid: ``{"calls": [...], "align_upper": [...], "persona_extra": int}``.

    ``fresh``: reframe / doc_classify / align are real calls (otherwise assumed cache hits, absent).  doc_claim is fetched
    once per bid with the REAL ``fetch_doc_claims`` (its chunks are the runner's token-aware batches).  ``slots`` =
    ``[(persona_seed, global_scenario_index), ...]`` for the persona calls.  ``align_upper``: the align calls if reframe
    dropped nothing (the ceiling).
    """
    from gtgen.data.scenario_build import fetch_doc_claims, merged_constraints
    all_cons = list(prep.brief.constraints)
    cand_cons = [cc for cv in prep.catalogue.candidates for cc in cv.constraints]
    calls: list[dict] = []
    upper: list[dict] = []
    items: dict[str, dict] = {}

    def _split(n_items, n_rule=0, n_pre=0, n_llm=None):
        return {"n_items": int(n_items), "n_rule": int(n_rule), "n_prefilter": int(n_pre),
                "n_llm": int(n_items - n_rule - n_pre if n_llm is None else n_llm)}
    _, split_r = _runners(settings)
    if fresh:
        items["reframe"] = _split(len(all_cons))            # reframe is always LLM
        sp = split_r.rule_split("doc_classify", all_cons)
        items["doc_classify"] = {k: int(sp[k]) for k in ("n_items", "n_rule", "n_prefilter", "n_llm")}
        c, r = _runners(settings)
        r.reframe(prep.product_category, all_cons)
        calls += _batched(c, settings)
        c, r = _runners(settings)
        r.doc_classify(all_cons)
        calls += _batched(c, settings)
        act = list(prep.active_constraints)
        sp = split_r.rule_split("align", act, cand_cons) if act and cand_cons else {"n_items": 0, "n_rule": 0, "n_prefilter": 0, "n_llm": 0}
        items["align"] = {k: int(sp[k]) for k in ("n_items", "n_rule", "n_prefilter", "n_llm")}
        for key, cons in (("align", list(prep.active_constraints)), ("upper", all_cons)):
            c, r = _runners(settings)
            if cons and cand_cons:                       # same guard as cli.prepare_bid
                r.align(prep.bid_id, cons, cand_cons)
            if key == "align":
                calls += _batched(c, settings)
            else:
                upper = _batched(c, settings)
    c, r = _runners(settings)
    claims = fetch_doc_claims(merged_constraints(prep, settings), prep.catalogue, r, settings, gate=None)
    calls += _batched(c, settings)
    cs = dict(getattr(claims, "stats", None) or {})         # D071: the real dedup / prefilter / rule / LLM split
    items["doc_claim"] = {"n_items": int(cs.get("pairs", 0)), "n_rule": int(cs.get("rule", 0)),
                          "n_prefilter": int(cs.get("prefiltered", 0)), "n_llm": int(cs.get("llm_items", 0)),
                          "unique": int(cs.get("unique", 0)), "deduped": int(cs.get("deduped", 0)),
                          "cache_hits": int(cs.get("cache_hits", 0))}
    pers = _persona_records(prep, settings, slots, n_scenarios, run_key)
    calls += pers
    items["persona"] = _split(len(pers))                    # persona is always LLM (one call per scenario)
    return {"calls": calls, "align_upper": upper, "items": items,
            "persona_extra_per_seed": int(((settings.get("persona", {}) or {}).get("diversity", {}) or {})
                                          .get("max_company_regenerations", 0))}


# ------------------------------------------------------------------ aggregation
def group_stats(records: Sequence[Mapping], budget: int, min_completion: int) -> dict:
    """calls, batch sizes, token totals, max / mean estimated total per call, calls over the budget."""
    if not records:
        return {"calls": 0, "batches": {}, "est_input_total": 0, "est_completion_total": 0, "max_total": 0,
                "mean_total": 0.0, "max_input": 0, "over_budget": 0}
    totals = [r["est_in"] + r["est_out"] for r in records]
    over = sum(1 for r in records if r["est_in"] + max(r["est_out"], min_completion) > budget)
    sizes = Counter(r["size"] for r in records if r.get("size") is not None)
    return {"calls": len(records), "batches": dict(sorted(sizes.items(), reverse=True)),
            "est_input_total": sum(r["est_in"] for r in records),
            "est_completion_total": sum(r["est_out"] for r in records),
            "max_total": max(totals), "mean_total": round(statistics.fmean(totals), 1),
            "max_input": max(r["est_in"] for r in records), "over_budget": over}


def table_report(per_bid: Mapping[str, dict], settings: Mapping, fresh: bool, personas: int) -> dict:
    """Per prompt (all bids) and per bid statistics + wall-clock at the configured pacing."""
    budget = int(settings["llm"]["max_tokens_per_call"])
    minc = int(settings["llm"]["min_completion_tokens"])
    pace = float(settings["llm"]["rate_limit"]["min_interval_s"])
    f = http_factors(settings)
    prompts = TABLE_PROMPTS if fresh else ("doc_claim", "persona")
    by_prompt: dict[str, dict] = {}
    by_bid: dict[str, dict] = {}
    allrec = [r for d in per_bid.values() for r in d["calls"]]
    for p in prompts:
        by_prompt[p] = group_stats([r for r in allrec if r["prompt"] == p], budget, minc)
        by_prompt[p].update({k: sum(d.get("items", {}).get(p, {}).get(k, 0) for d in per_bid.values())
                             for k in ("n_items", "n_rule", "n_prefilter", "n_llm", "unique", "deduped", "cache_hits")})
    for bid, d in per_bid.items():
        by_bid[bid] = {p: group_stats([r for r in d["calls"] if r["prompt"] == p], budget, minc) for p in prompts}
    n = len(allrec)
    extra = sum(d["persona_extra_per_seed"] * sum(1 for r in d["calls"] if r["prompt"] == "persona")
                for d in per_bid.values())
    upper = n + extra + (sum(len(d["align_upper"]) - sum(1 for r in d["calls"] if r["prompt"] == "align")
                             for d in per_bid.values()) if fresh else 0)
    return {"budget": budget, "min_completion": minc, "pacing_s": pace, "fresh": fresh, "by_prompt": by_prompt,
            "by_bid": by_bid, "logical_calls": n, "upper_calls": upper, "worst_http_calls": upper * f["per_call"],
            "token_limit_total": int(settings["llm"].get("token_limit_total", 0)),
            "max_completion": int(settings["llm"]["max_completion_tokens"]), "factors": f,
            "logical_minutes": n * pace / 60.0, "worst_http_minutes": worst_minutes(upper, f),
            "backoff_minutes_if_every_call_fails_once": n * f["backoff_s"] / 60.0,
            "est_input_total": sum(s["est_input_total"] for s in by_prompt.values()),
            "est_completion_total": sum(s["est_completion_total"] for s in by_prompt.values()),
            "max_total": max((s["max_total"] for s in by_prompt.values()), default=0),
            "over_budget": sum(s["over_budget"] for s in by_prompt.values())}


def _fmt_sizes(b: Mapping) -> str:
    return ", ".join(f"{k}x{v}" for k, v in b.items()) if b else "n/a"


def render_table_report(rep: Mapping) -> list[str]:
    bud = rep["budget"]
    out = [f"PREP PATH REPORT (D066 deterministic-first; items resolved by rule / prefilter never reach the LLM; calls = batches "
           f"actually sent, first attempt):", "",
           "| prompt | items | rule-resolved | prefilter-resolved | LLM items | LLM calls (batches) |", "|---|---|---|---|---|---|"]
    tot = [0, 0, 0, 0, 0]
    for p, s in rep["by_prompt"].items():
        row = [s.get("n_items", 0), s.get("n_rule", 0), s.get("n_prefilter", 0), s.get("n_llm", 0), s["calls"]]
        tot = [a + b for a, b in zip(tot, row)]
        out.append(f"| {p} | " + " | ".join(str(x) for x in row) + " |")
    out += ["| TOTAL | " + " | ".join(str(x) for x in tot) + " |", ""]
    dc = rep["by_prompt"].get("doc_claim")
    if dc:
        out += [f"doc_claim (D071, from fetch_doc_claims stats): pairs {dc.get('n_items', 0)} = prefiltered {dc.get('n_prefilter', 0)} + "
                f"unique (listing, standard) keys {dc.get('unique', 0)} + deduped {dc.get('deduped', 0)}; of the unique keys "
                f"rule-resolved {dc.get('n_rule', 0)}, LLM items {dc.get('n_llm', 0)} in {dc['calls']} call(s).", ""]
    out += [f"TOKEN REPORT (estimates from the real rendered prompts; budget {bud} tokens per call incl. completion, "
           f"endpoint context {rep.get('token_limit_total', 'n/a')} tokens input + output, output <= {rep.get('max_completion', 'n/a')}):", "",
           "| prompt | calls | batch sizes (size x n) | est. input tokens | est. completion tokens | max total/call | "
           "mean total/call | max vs budget | calls over budget |", "|" + "---|" * 9]
    for p, s in rep["by_prompt"].items():
        out.append(f"| {p} | {s['calls']} | {_fmt_sizes(s['batches'])} | {s['est_input_total']} | {s['est_completion_total']} | "
                   f"{s['max_total']} | {s['mean_total']} | {100.0 * s['max_total'] / bud:.0f}% | {s['over_budget']} |")
    out.append(f"| TOTAL | {rep['logical_calls']} | | {rep['est_input_total']} | {rep['est_completion_total']} | "
               f"{rep['max_total']} | | {100.0 * rep['max_total'] / bud:.0f}% | {rep['over_budget']} |")
    f = rep["factors"]
    out += ["", f"Wall clock at min_interval_s={rep['pacing_s']} (20 rpm + margin), excluding model latency: logical "
            f"{rep['logical_calls']} calls = {rep['logical_minutes']:.1f} min (+ {f['backoff_s']:.0f} s backoff per fully "
            f"failing call: {rep['backoff_minutes_if_every_call_fails_once']:.1f} min if every call failed once). HTTP worst "
            f"case = upper_calls x V x H with V={f['V']} validation attempts, H={f['H']} HTTP attempts: {rep['upper_calls']} x "
            f"{f['per_call']} = {rep['worst_http_calls']} requests = {rep['worst_http_minutes']:.1f} min (pacing + V x "
            f"{f['backoff_s']:.0f} s backoff per call)."]
    return out


# ------------------------------------------------------------------ dialogue: call count as a function of the brief
@dataclass(frozen=True)
class DialogueParams:
    """What the number of LLM calls of one negotiation depends on (all from the brief / settings)."""
    n_voiced: int                 # constraints the buyer can ever say (non DROP/MERGE/DROPPED, incl. synthetic terms)
    n_open: int                   # constraints stated in the opening: opening_min + seed % (opening_max - opening_min + 1)
    hard_cap: int                 # negotiation.max_turns_hard_cap
    reveal_batch: int             # dialogue.progressive_reveal.reveal_max_per_answer
    clarify_max: int              # D072: rounds ALLOWED for this brief = clarify_max_rounds, or clarify_max_rounds_complex when n_open >= clarify_complex_min_stated
    claim_calls_exp: int = 0      # dialogue doc_claim calls expected (doc constraints revealed by the clarify rounds)
    claim_calls_max: int = 0      # every doc wording believed, each in its own step
    p_expected: int = 3           # proposal rounds expected (assumption, see dialogue_call_range)
    proof_rate: float = 0.25      # mean of proof_requests.rate_by_tier
    reveal_enabled: bool = True

    @property
    def unrevealed(self) -> int:
        return max(0, self.n_voiced - self.n_open)


@dataclass(frozen=True)
class DialogueCounts:
    """One negotiation, in events.  Every field maps to LLM calls exactly as the loop makes them:

    P  answered proposal rounds: negotiator_reply + buyer_reply + negotiator_interpret each (3 calls)
    A  acknowledgements (D055 proof requests): negotiator_acknowledge (1 call); A <= P
    C  clarify rounds (D072): negotiator_clarify + buyer_reveal (2 calls); buyer.reveal() then runs reveal_check iff constraints
       are still unrevealed (rc; a clarify answer never reveals anything, so unrevealed stays > 0 and rc = C then).  Each round is
       followed by one negotiator_extract of the buyer's answer, which is counted in the per-step extract below.
    Rp proposal answers that also reveal constraints: buyer_reveal + reveal_check (rp_chk)
    u  1 if the turn cap cuts off the last proposal unanswered: one more negotiator_reply, no buyer_reply / interpret
    D  doc_claim calls inside the negotiator (D051)
    Every negotiator step (P + u + A + C + the final RECOMMEND / BEST_AVAILABLE step) calls negotiator_extract once; the
    opening is buyer_opening + reveal_check (1 + 1).  The final RECOMMEND / BEST_AVAILABLE turn is one negotiator_final call (D069); WRAP_UP text is deterministic (0 calls).
    """
    P: int = 0
    A: int = 0
    C: int = 0
    rc: int = 0
    Rp: int = 0
    rp_chk: int = 0
    u: int = 0
    D: int = 0

    def calls(self, has_constraints: bool = True) -> Counter:
        c: Counter = Counter()
        c["buyer_opening"] = 1
        c["reveal_check"] = (1 if has_constraints else 0) + self.rc + self.rp_chk
        c["negotiator_extract"] = self.P + self.u + self.A + self.C + 1
        c["negotiator_reply"] = self.P + self.u
        c["buyer_reply"] = self.P
        c["negotiator_interpret"] = self.P
        c["negotiator_acknowledge"] = self.A
        c["negotiator_clarify"] = self.C
        c["buyer_reveal"] = self.C + self.Rp
        c["doc_claim"] = self.D
        c["negotiator_final"] = 1       # D069: RECOMMEND / BEST_AVAILABLE wording, exactly one call per finished conversation
        return +c

    def total(self, has_constraints: bool = True) -> int:
        return sum(self.calls(has_constraints).values())

    def turns(self) -> int:
        """Turns before the wrap-up / final step: opening + 2 per answered proposal + 1 per ack + 2 per clarify (+ u)."""
        return 1 + 2 * self.P + self.A + 2 * self.C + self.u


def expected_rc(p: DialogueParams, C: int) -> int:
    """D072: ``Buyer.reveal`` calls reveal_check after every clarify answer iff ``unrevealed()`` is non-empty; a clarify answer
    reveals nothing new (plan_reveal only restates already-stated constraints), so the set stays non-empty for every round."""
    if not p.reveal_enabled or p.unrevealed <= 0:
        return 0
    return C


def dialogue_call_range(p: DialogueParams) -> dict[str, DialogueCounts]:
    """min / expected / max event counts for one negotiation.

    min       the buyer's brief leaves no relaxable option: opening + one negotiator step that ends BEST_AVAILABLE (4 calls).
    min_neg   at least one proposal answered, no ack, no clarify.
    expected  P = ``p_expected`` (assumption: accepting rounds ~ mid of persona max_relaxations [0,3] = 1.5 -> 2, plus ~1
              refused / countered round), A = round(P * proof_rate * 1.5 items), C = 1 clarify round when the brief allows one
              (D072: clarify is SKIPPED when nothing is ambiguous, so C=0 is also common - the golden conversation has C=0 -
              and the second round of a complex brief needs ambiguity left after the first answer; both are in the max, not
              the expected), rc from ``expected_rc``, no reveal inside proposal answers (Rp = 0), no cap cut-off,
              D = ``claim_calls_exp`` (0 when the table's claim cache is shared, D071).
    max       maximum over every (P, A, C, Rp, u) that fits ``hard_cap`` turns (A <= P, C <= clarify_max, Rp <= min(P, unrevealed),
              rc = C, rp_chk = Rp), D = ``claim_calls_max``.  The turn cap is the only hard bound on P: patience resets on
              every YES and a rule may be raised through several levels.
    """
    cmax = p.clarify_max if p.reveal_enabled else 0
    min_c = DialogueCounts()
    min_neg = DialogueCounts(P=1)
    P = max(0, p.p_expected)
    A = int(round(P * p.proof_rate * 1.5))
    C = min(1, cmax)
    exp = DialogueCounts(P=P, A=min(A, P), C=C, rc=expected_rc(p, C), D=p.claim_calls_exp)
    best, best_n = DialogueCounts(D=p.claim_calls_max), -1
    for Pm in range(0, p.hard_cap + 1):
        for Am in range(0, Pm + 1):
            for Cm in range(0, cmax + 1):
                for Rp in range(0, min(Pm, p.unrevealed if p.reveal_enabled else 0) + 1):
                    for u in (0, 1):
                        cand = DialogueCounts(P=Pm, A=Am, C=Cm, rc=expected_rc(p, Cm), Rp=Rp,
                                              rp_chk=Rp if p.unrevealed > 0 else 0, u=u, D=p.claim_calls_max)
                        t = cand.turns()
                        if t > p.hard_cap or (u and t != p.hard_cap):
                            continue
                        n = cand.total()
                        if n > best_n:
                            best, best_n = cand, n
    return {"min": min_c, "min_neg": min_neg, "expected": exp, "max": best}


# ------------------------------------------------------------------ dialogue: tokens per prompt
_VOCAB = ("supplier delivery pump quality warranty price certified standard rated power motor capacity stock dispatch "
          "terms payment lead time order quantity budget requirement confirm shortlist option offer listing evidence "
          "site audit report review").split()


def _prose(n_words: int, salt: int = 0) -> str:
    """Deterministic English-like text of ``n_words`` words (the estimator only sees characters)."""
    words = [_VOCAB[(i * 7 + salt) % len(_VOCAB)] for i in range(n_words)]
    out = []
    for i in range(0, n_words, 14):
        s = " ".join(words[i:i + 14])
        out.append(s[:1].upper() + s[1:] + ".")
    return " ".join(out)


def _label(c) -> str:
    return c.commercial_text or c.raw_text or c.attribute


@dataclass
class _Ctx:
    prep: Any
    settings: Mapping
    voiced: list           # BuyerConstraint in priority order (real ones)
    n_synth: int
    n_open: int
    sales: dict
    profile: Any
    persona: Any
    runners: Any
    client: Any
    neg: Any


def _make_ctx(prep, settings: Mapping, n_open: int) -> _Ctx:
    from gtgen.contracts import BuyerProfile, Persona, Tier
    from gtgen.data.scenario import commercial_bands
    from gtgen.data.scenario_build import merged_constraints
    from gtgen.dialogue._reveal import priority_order
    from gtgen.dialogue.negotiator import Negotiator
    from gtgen.llm import LLMRunners
    cons = merged_constraints(prep, settings)
    voiced = priority_order(cons, settings)
    bands = commercial_bands(prep.catalogue, float(prep.brief.quantity), settings)
    sales = {"name": "Karthik", "speaking_style": "Warm and unhurried, uses the buyer's own words.",
             "sales_approach": "Eager to close but only states facts the listing backs up."}
    profile = BuyerProfile(company_name="Sri Lakshmi Precision Industries", industry="Industrial fabrication",
                           city="Coimbatore", style="Formal and terse, wants numbers first and dislikes small talk.",
                           llm_call_id="plan")
    persona = Persona(persona_seed=7, tau=0.6, tier=Tier.MEDIUM_STRICT, category_tolerance={"PRODUCT": 1.0},
                      counter_band=0.2, patience_turns=8, ranking_weights={"spec_match": 1.0}, llm_call_id="plan")
    client, runners = _runners(settings)
    neg = Negotiator(prep.catalogue, runners, dict(settings), alignments=[], verification_roles=True,
                     sales_persona=sales, cache_seed=1)
    return _Ctx(prep, settings, voiced, len(bands["terms"]), n_open, sales, profile, persona, runners, client, neg)


def _transcript(prep, ctx: _Ctx, n_turns: int, words: int, opening_text: str):
    from gtgen.contracts import Speaker, TranscriptTurn, TranscriptView
    turns = [TranscriptTurn(turn_idx=0, speaker=Speaker.BUYER, utterance=opening_text)]
    for i in range(1, n_turns):
        turns.append(TranscriptTurn(turn_idx=i, speaker=Speaker.NEGOTIATOR if i % 2 else Speaker.BUYER,
                                    utterance=_prose(words, i)))
    return TranscriptView(scenario_id="plan", turns=turns)


def _measure(ctx: _Ctx, fn) -> int:
    ctx.client.records.clear()
    fn()
    return max(r["est_in"] for r in ctx.client.records) if ctx.client.records else 0


def dialogue_prompt_tokens(prep, settings: Mapping, n_open: int = 6) -> dict[str, dict]:
    """Per dialogue prompt: ``typical`` and ``max`` estimated input tokens, expected completion, max total.

    Payloads come from the bid's real data: requirement texts are the real (reframed) constraints in reveal priority order,
    evidence snippets are built by the Negotiator's own ``_evidence`` from the real candidates (typical = first 8 candidates;
    max = the 8 with the longest snippets, plus a proof note), clarify topics by its own ``_clarify_topics``.  Transcript
    utterances are synthetic prose: typical 9 turns x 80 words, max 22 turns (turn cap 20 + wrap-up + final) x 140 words
    ("3 to 6 sentences" per prompt).  ``max`` also assumes every constraint is revealed and believed.
    """
    from gtgen.contracts import BelievedConstraint, Hardness
    from gtgen.llm import BuyerItemDecision, ProofRequestView, RequirementStatement
    from gtgen.llm import MenuItemView, MenuPointView
    x = _make_ctx(prep, settings, n_open)
    budget = int(settings["llm"]["max_tokens_per_call"])
    minc = int(settings["llm"]["min_completion_tokens"])
    voiced = x.voiced
    n_all = max(1, len(voiced)) + x.n_synth
    labels = [_label(c) for c in voiced] or ["Delivery within 10 days"]
    stren = ["must" if c.hardness == Hardness.HARD else "prefer" for c in voiced] or ["prefer"]
    reqs_open = [RequirementStatement(text=t, strength=s) for t, s in list(zip(labels, stren))[:n_open]]
    opening_text = (" ".join(r.text + "." for r in reqs_open) + " " + _prose(40))
    cat = prep.catalogue
    snips = sorted(cat.candidates, key=lambda c: -len(x.neg._snippet_text(c)))
    ev_typ = x.neg._evidence(cat, [c.product_id for c in cat.candidates[:8]])
    ev_max = x.neg._evidence(cat, [c.product_id for c in snips[:8]],
                             {c.product_id: "The listing states test report: " + _prose(30) for c in snips[:2]})
    topics = x.neg._clarify_topics(cat, [], {})
    stated_typ = labels[:n_open + 4]
    stated_max = list(labels) + ["Budget of up to Rs 1,250,000 in total"] * x.n_synth
    menu = MenuPointView(items=[MenuItemView(attribute=t[:40], kind="numeric_widen", from_value="24 months",
                                             to_value="21.6 months", note="10% looser than the stated requirement")
                                for t in labels[:4]], extra_options=3)
    known_typ = [t[:40] for t in labels[:n_open + 4]]
    known_max = [t[:40] for t in labels]
    items_typ = [BuyerItemDecision(item=labels[0], action="YES", proposed_value="21.6 months")]
    items_max = [BuyerItemDecision(item=t, action="COUNTER", strength="prefer", proposed_value="21.6 months",
                                   counter_value="22.5 months") for t in labels[:4]] + \
                [BuyerItemDecision(item=labels[0], action="PROOF_REQUEST", doc_name="test report",
                                   claimed="Supplier lists it as meeting this requirement") for _ in range(2)]
    prv = lambda n: [ProofRequestView(attribute=labels[0][:40], doc_name="test report", evidence_id="E1")  # noqa: E731
                     for _ in range(n)]
    r = x.runners
    sp = x.sales
    res: dict[str, dict] = {}

    def case(typ: bool, words: Optional[int] = None) -> dict[str, int]:
        w = words if words is not None else (80 if typ else 140)
        t = _transcript(prep, x, 9 if typ else 22, w, opening_text)
        ev = ev_typ if typ else ev_max
        stated = stated_typ if typ else stated_max
        known = known_typ if typ else known_max
        items = items_typ if typ else items_max
        out = {}
        out["buyer_opening"] = _measure(x, lambda: r.buyer_opening(
            x.profile, prep.product_category, float(prep.brief.quantity), prep.brief.quantity_unit, 1250000.0,
            reqs_open if typ else [RequirementStatement(text=t, strength="prefer") for t in labels[:7]],
            persona=x.persona))
        pend = (voiced[n_open:] if typ else voiced) or voiced[:1]
        if not pend:
            from gtgen.contracts import BuyerConstraint
            pend = [BuyerConstraint(constraint_id="X", bid_id="X", golden_param=False, hardness=Hardness.SOFT,
                                    attribute="delivery", commercial_text=labels[0])]
        out["reveal_check"] = _measure(x, lambda: r.reveal_check(_prose(w), pend))
        out["negotiator_extract"] = _measure(x, lambda: r.negotiator_extract(t))
        out["negotiator_interpret"] = _measure(x, lambda: r.negotiator_interpret(
            t, [{"index": i, "attribute": "warranty", "kind": "numeric_widen", "from_value": "24 months",
                 "to_value": "21.6 months"} for i in range(1 if typ else 4)]))
        out["negotiator_reply"] = _measure(x, lambda: r.negotiator_reply(
            t, None, [], ev, sales_persona=sp, menu_point=menu, allowed_numbers=[3, 10, 21.6, 24]))
        out["buyer_reply"] = _measure(x, lambda: r.buyer_reply(x.profile, t, "YES" if typ else "MIXED", items,
                                                               persona=x.persona, already_stated=stated))
        out["negotiator_acknowledge"] = _measure(x, lambda: r.negotiator_acknowledge(
            t, prv(1 if typ else 2), ev, sales_persona=sp))
        out["negotiator_clarify"] = _measure(x, lambda: r.negotiator_clarify(t, topics, known, sales_persona=sp))
        out["buyer_reveal"] = _measure(x, lambda: r.buyer_reveal(
            x.profile, t, reqs_open[:4], _prose(w), persona=x.persona, already_stated=stated))
        top = cat.candidates[:3]
        facts = {"options": [{"rank": i + 1, "product": c.product_name, "supplier": c.supplier_name or "the supplier",
                              "price_text": c.price_formatted or "price on request",
                              "met": labels[:3] if typ else labels, "unmet": labels[3:4] if typ else labels[:3],
                              "unverified": labels[:1]} for i, c in enumerate(top)],
                 "to_confirm": labels[:2] if typ else labels[:6], "note": _prose(20 if typ else 40)}
        def _final():
            try:        # the dry-run client never answers; the runner asserts after the call is recorded
                r.negotiator_final("recommend", facts, sales_persona=sp)
            except AssertionError:
                pass
        out["negotiator_final"] = _measure(x, _final)
        return out

    typ, mx = case(True), case(False)
    lo = case(False, 60)            # slope per word of a full-length transcript -> words per utterance at the input limit
    utter_t, utter_m = 130, 250             # tokens of one 80 / 140 word reply plus JSON wrapper
    comp = {
        "buyer_opening": (utter_t + 40, utter_m + 60), "reveal_check": (20, 20 + 8 * min(n_all, 30)),
        "negotiator_extract": (55 * (n_open + 4) + 10, 55 * n_all + 10), "negotiator_interpret": (35, 4 * 35 + 10),
        "negotiator_reply": (utter_t + 30, utter_m + 40), "buyer_reply": (utter_t, utter_m),
        "negotiator_acknowledge": (utter_t + 30, utter_m + 40), "negotiator_clarify": (utter_t + 40, utter_m + 80),
        "buyer_reveal": (utter_t, utter_m), "negotiator_final": (utter_t + 60, utter_m + 100)}
    for k in typ:
        ct, cm = comp[k]
        res[k] = {"typical_in": typ[k], "max_in": mx[k], "typical_out": ct, "max_out": cm,
                  "typical_total": typ[k] + ct, "max_total": mx[k] + cm,
                  "max_input_ok": mx[k] + max(cm, minc) <= budget, "max_input_limit": budget - minc}
        slope = (mx[k] - lo[k]) / 80.0
        res[k]["words_per_utterance_at_limit"] = (int(140 + (budget - minc - mx[k]) / slope)
                                                   if slope > 1.0 and k != "reveal_check" else None)
    return res


# ------------------------------------------------------------------ dialogue: bid-level model
def claim_call_model(prep, settings: Mapping, n_open: int, clarify_rounds: int, shared_cache: bool = True) -> dict:
    """Dialogue doc_claim calls per conversation from the REAL batch planner, per doc wording (memo per wording, D051).

    D071: with ``shared_cache`` (the table-stage ClaimCache is passed to ``run_dialogue``) the dialogue only sends pairs whose
    (listing hash, standard) is not in the table cache.  The negotiator's beliefs come from the buyer's own statements, which
    are the brief's constraints, so every pair it needs was already classified at the table stage: ``exp`` = 0.  ``max`` keeps
    the no-cache worst case (a miss for EVERY wording: new beliefs naming a standard absent from the table brief, or a changed
    listing); ``exp_uncached`` is the old D051 expectation, reported for reference.

    The negotiator asks for claims when a doc-proof belief appears; each wording that appears in a different step is a
    separate ``runners.doc_claims`` call (batched by tokens).  ``exp``: wordings among the first ``n_open + 4 * clarify_rounds``
    constraints in reveal priority; ``max``: every wording separately.  ``one_shot``: all wordings in a single step.
    Also returns the average estimated tokens of those calls."""
    from gtgen.data.scenario_build import _doc_constraints, claim_requests, merged_constraints
    from gtgen.dialogue._reveal import priority_order
    cons = merged_constraints(prep, settings)
    docs = _doc_constraints(cons)
    if not docs:
        return {"exp": 0, "exp_uncached": 0, "max": 0, "one_shot": 0, "wordings": 0, "avg_tokens": 0, "per_wording": {}}
    _c, r = _runners(settings)
    by_word: dict[str, list] = {}
    pairs, reqs = claim_requests(cons, prep.catalogue, settings)
    cid_word = {c.constraint_id: " ".join((c.doc_name or c.attribute).lower().split()) for c in docs}
    for q in reqs:
        by_word.setdefault(cid_word[q.constraint_ref], []).append(q)
    per = {w: r.plan_doc_claims(qs) for w, qs in by_word.items()}
    order = [c.constraint_id for c in priority_order(cons, settings)]
    seen = set(order[:n_open + 4 * clarify_rounds])
    words_exp = {cid_word[c.constraint_id] for c in docs if c.constraint_id in seen}
    allq = [q for qs in by_word.values() for q in qs]
    one = r.plan_doc_claims(allq)
    toks = [pb.est_input_tokens + pb.est_completion_tokens for pbs in per.values() for pb in pbs]
    exp_unc = sum(len(per.get(w, [])) for w in words_exp)
    return {"exp": 0 if shared_cache else exp_unc, "exp_uncached": exp_unc, "shared_cache": shared_cache, "max": sum(len(v) for v in per.values()),
            "one_shot": len(one), "wordings": len(by_word), "per_wording": {w: len(v) for w, v in per.items()},
            "avg_tokens": int(statistics.fmean(toks)) if toks else 0}


def dialogue_params_for(prep, settings: Mapping, persona_seed: int) -> DialogueParams:
    from gtgen.data.scenario import commercial_bands
    from gtgen.data.scenario_build import merged_constraints
    from gtgen.dialogue._reveal import opening_size, priority_order
    pr = settings["dialogue"]["progressive_reveal"]
    cons = merged_constraints(prep, settings)
    voiced = priority_order(cons, settings)
    n_synth = len(commercial_bands(prep.catalogue, float(prep.brief.quantity), settings)["terms"])
    n_all = len(voiced) + n_synth
    n_open = min(n_all, int(pr["opening_min"]) + persona_seed % (int(pr["opening_max"]) - int(pr["opening_min"]) + 1))
    # D077: the negotiator never clarifies any more, so the rounds allowed for THIS brief is always 0 (kept as a
    # read of the settings, degenerated to 0, rather than dropping the keys or the formulas below that take it).
    cmax = 0
    cm = claim_call_model(prep, settings, n_open, cmax)
    rates = settings.get("proof_requests", {}).get("rate_by_tier", {})
    return DialogueParams(
        n_voiced=n_all, n_open=n_open, hard_cap=int(settings["negotiation"]["max_turns_hard_cap"]),
        reveal_batch=int(pr["reveal_max_per_answer"]), clarify_max=cmax,
        claim_calls_exp=cm["exp"], claim_calls_max=cm["max"],
        proof_rate=statistics.fmean(rates.values()) if rates else 0.25, reveal_enabled=bool(pr["enabled"]))


def dialogue_report(prep, settings: Mapping, seeds: Sequence[int]) -> dict:
    """Per-bid dialogue estimate for ``seeds`` (one conversation each): call ranges, tokens, budget check."""
    tokens = dialogue_prompt_tokens(prep, settings, n_open=6)
    cm_avg = claim_call_model(prep, settings, 6, int(settings['dialogue']['progressive_reveal']['clarify_max_rounds_complex']))
    budget = int(settings["llm"]["max_tokens_per_call"])
    scen = []
    for s in seeds:
        p = dialogue_params_for(prep, settings, s)
        rng = dialogue_call_range(p)
        row = {"seed": s, "params": p.__dict__ | {"unrevealed": p.unrevealed}}
        for k, cnt in rng.items():
            calls = cnt.calls()
            tot_tokens = 0
            for name, n in calls.items():
                if name == "doc_claim":
                    tot_tokens += n * cm_avg["avg_tokens"]
                else:
                    t = tokens[name]
                    tot_tokens += n * (t["max_total"] if k == "max" else t["typical_total"])
            row[k] = {"counts": cnt.__dict__, "calls": dict(calls), "total": sum(calls.values()), "tokens": tot_tokens}
        scen.append(row)
    biggest = max(tokens.items(), key=lambda kv: kv[1]["max_in"])
    return {"tokens": tokens, "scenarios": scen, "claims": cm_avg, "budget": budget,
            "biggest": {"prompt": biggest[0], "max_in": biggest[1]["max_in"], "max_total": biggest[1]["max_total"]},
            "any_over_budget": any(not t["max_input_ok"] for t in tokens.values())}


ROLE_ORDER = ("prep", "claims", "persona", "buyer", "negotiator")


def role_wallclock(calls_tokens: Mapping[str, tuple], settings: Mapping) -> dict:
    """D068: per API-key role, minutes = max(tokens / (tpm_limit*tpm_safety), calls / rpm).  ``calls_tokens`` maps prompt ->
    (calls, est input+output tokens).  Roles use separate keys so they could overlap; in practice they run sequentially (sum)."""
    from gtgen.llm.client import role_for_prompt
    llm = settings["llm"]
    budget = float(llm["tpm_limit"]) * float(llm["tpm_safety"])
    rpm = float(llm["rate_limit"].get("requests_per_minute", 30))
    roles: dict[str, dict] = {r: {"calls": 0, "tokens": 0} for r in ROLE_ORDER}
    for prompt, (c, t) in calls_tokens.items():
        r = roles.setdefault(role_for_prompt(prompt) or "default", {"calls": 0, "tokens": 0})
        r["calls"] += c
        r["tokens"] += t
    for r in roles.values():
        r["minutes_tpm"] = r["tokens"] / budget
        r["minutes_rpm"] = r["calls"] / rpm
        r["minutes"] = max(r["minutes_tpm"], r["minutes_rpm"])
    return {"roles": roles, "tpm_budget": budget, "rpm": rpm, "sum_minutes": sum(r["minutes"] for r in roles.values()),
            "max_minutes": max(r["minutes"] for r in roles.values())}


def stage1_totals(table: Mapping, dialogue_by_bid: Mapping[str, dict], settings: Mapping) -> dict:
    """Table stage + dialogue for every scenario: calls, minutes (pacing), tokens, for min / expected / max."""
    pace = float(settings["llm"]["rate_limit"]["min_interval_s"])
    f = http_factors(settings)
    out = {}
    for k in ("min", "expected", "max"):
        d_calls = sum(sc[k]["total"] for d in dialogue_by_bid.values() for sc in d["scenarios"])
        d_tok = sum(sc[k]["tokens"] for d in dialogue_by_bid.values() for sc in d["scenarios"])
        d_claims = sum(sc[k]["calls"].get("doc_claim", 0) for d in dialogue_by_bid.values() for sc in d["scenarios"])
        t_calls = table["upper_calls"] if k == "max" else table["logical_calls"]
        t_tok = table["est_input_total"] + table["est_completion_total"]
        calls = t_calls + d_calls
        ct: dict[str, list] = {}
        for p_, bp in table["by_prompt"].items():
            ct.setdefault(p_, [0, 0])
            ct[p_][0] += bp["calls"]
            ct[p_][1] += bp["est_input_total"] + bp["est_completion_total"]
        for d in dialogue_by_bid.values():
            for sc in d["scenarios"]:
                for name, n in sc[k]["calls"].items():
                    tk = d["claims"]["avg_tokens"] if name == "doc_claim" else \
                        d["tokens"][name]["max_total" if k == "max" else "typical_total"]
                    ct.setdefault(name, [0, 0])
                    ct[name][0] += n
                    ct[name][1] += n * tk
        wall = role_wallclock({a: tuple(b) for a, b in ct.items()}, settings)
        out[k] = {"wallclock": wall, "by_prompt_calls_tokens": ct, "table_calls": t_calls, "dialogue_calls": d_calls, "calls": calls, "minutes": calls * pace / 60.0,
                  "http_worst_calls": calls * f["per_call"], "http_worst_minutes": worst_minutes(calls, f),
                  "dialogue_doc_claim_calls": d_claims, "calls_if_table_claims_reused": calls - d_claims,
                  "tokens": t_tok + d_tok}
    return out


def render_dialogue(dlg: Mapping[str, dict], settings: Mapping) -> list[str]:
    budget = int(settings["llm"]["max_tokens_per_call"])
    minc = int(settings["llm"]["min_completion_tokens"])
    out = ["", "DIALOGUE (NOT in the scenario table; one negotiation per scenario; calls from the real turn loop, tokens from "
           "the real prompt templates with payloads sized from each bid):", ""]
    for bid, d in dlg.items():
        p0 = d["scenarios"][0]["params"]
        out += [f"### {bid}: voiced constraints {p0['n_voiced']} (incl. synthetic terms), opening states {p0['n_open']} "
                f"(seed-dependent), unrevealed after opening {p0['unrevealed']}, turn cap {p0['hard_cap']}, reveal batch "
                f"{p0['reveal_batch']}, clarify rounds <= {p0['clarify_max']} (D072; each = negotiator_clarify + buyer_reveal [+ reveal_check] + extract)", "",
                "| prompt | typical in | max in | typical out | max out | max total | vs budget | input limit "
                f"({budget - minc}) | utterance words at the limit |", "|" + "---|" * 9]
        for name, t in d["tokens"].items():
            out.append(f"| {name} | {t['typical_in']} | {t['max_in']} | {t['typical_out']} | {t['max_out']} | "
                       f"{t['max_total']} | {100.0 * t['max_total'] / budget:.0f}% | "
                       f"{'ok' if t['max_input_ok'] else 'EXCEEDS (LLMBudgetError)'} | "
                       f"{t.get('words_per_utterance_at_limit') or 'n/a'} |")
        cl = d["claims"]
        out += ["", f"doc_claim inside the dialogue (D071 shared claim cache): {cl['wordings']} wordings. Expected {cl['exp']} calls per "
                f"conversation (every needed (listing, standard) pair is a table-cache hit; without the cache it would be "
                f"{cl.get('exp_uncached', cl['exp'])}). Documented worst case {cl['max']} calls (a miss for every wording: beliefs naming a "
                f"standard absent from the table brief or a changed listing; one shot {cl['one_shot']}); per wording "
                f"{cl['per_wording']}; avg {cl['avg_tokens']} tokens/call (same token-aware planner as the table).", "",
                "| seed | min | min with proposal | expected | max | expected by prompt |", "|---|---|---|---|---|---|"]
        for sc in d["scenarios"]:
            out.append(f"| {sc['seed']} | {sc['min']['total']} | {sc['min_neg']['total']} | {sc['expected']['total']} | "
                       f"{sc['max']['total']} | {', '.join(f'{k} {v}' for k, v in sc['expected']['calls'].items())} |")
        b = d["biggest"]
        out += ["", f"Biggest dialogue prompt: {b['prompt']} = {b['max_in']} est. input tokens, {b['max_total']} total "
                f"(budget {budget}); " + ("every dialogue prompt fits." if not d["any_over_budget"] else
                "AT LEAST ONE EXCEEDS the input limit: the client raises LLMBudgetError before any HTTP request."), ""]
    return out


def render_stage1(plan: Mapping) -> list[str]:
    st = plan.get("stage1")
    if not st:
        return []
    n = len(plan["bids"]) * int(plan["personas"])
    out = ["STAGE-1 TOTAL (scenario table + dialogue, " + f"{n} scenarios = {len(plan['bids'])} bids x {plan['personas']} personas; "
           "first-attempt calls; informational, nothing is capped or refused, D066):", "",
           "| case | table calls | dialogue calls | total calls | minutes at pacing | HTTP worst (x V x H) | HTTP worst minutes "
           "| est. tokens | dialogue doc_claim calls (saved if the table's claims are passed in) |", "|" + "---|" * 9]
    for k in ("min", "expected", "max"):
        v = st[k]
        out.append(f"| {k} | {v['table_calls']} | {v['dialogue_calls']} | {v['calls']} | {v['minutes']:.1f} | "
                   f"{v['http_worst_calls']} | {v['http_worst_minutes']:.1f} | {v['tokens']} | {v['dialogue_doc_claim_calls']} |")
    out += ["", "WALL CLOCK per key role (D068): minutes = max(est tokens / tpm budget, calls / rpm); roles run sequentially in "
            "practice, so the overall figure is the SUM (the max would need all five keys busy at once).", ""]
    for k in ("min", "expected", "max"):
        w = st[k]["wallclock"]
        out += [f"{k}: tpm budget {w['tpm_budget']:.0f}/min per key, rpm {w['rpm']:.0f}", "",
                "| role | calls | est tokens | min by tokens | min by rpm | wall-clock min |", "|---|---|---|---|---|---|"]
        for r, v in w["roles"].items():
            out.append(f"| {r} | {v['calls']} | {v['tokens']} | {v['minutes_tpm']:.1f} | {v['minutes_rpm']:.1f} | {v['minutes']:.1f} |")
        out += [f"| overall: sequential SUM / parallel MAX | | | | | {w['sum_minutes']:.1f} / {w['max_minutes']:.1f} |", ""]
    out += ["", "Dialogue calls per bid (sum over its scenarios; min / expected / max):", "",
            "| bid | min | expected | max | doc_claim calls in expected / max |", "|---|---|---|---|---|"]
    for bid, d in plan["dialogue"].items():
        sc = d["scenarios"]
        out.append(f"| {bid} | {sum(x['min']['total'] for x in sc)} | {sum(x['expected']['total'] for x in sc)} | "
                   f"{sum(x['max']['total'] for x in sc)} | {sum(x['expected']['calls'].get('doc_claim', 0) for x in sc)} / "
                   f"{sum(x['max']['calls'].get('doc_claim', 0) for x in sc)} |")
    out += ["", "min = every conversation ends after its first negotiator step; max = the turn-cap worst case for every "
            "scenario (table: upper bound incl. persona regenerations); tokens: expected/min use typical payloads, max uses the "
            "maximal payloads.", ""]
    return out
