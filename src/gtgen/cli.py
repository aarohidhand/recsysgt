"""gtgen command-line interface. OWNER: main session (coordinator).

Usage:
    python -m gtgen.cli pilot --mock --bid GEM/2019/B/242710
    python -m gtgen.cli pilot --mock --bid GEM/2019/B/242710 --bid GEM/2019/B/231682   # multiple bids, 1 persona each
    python -m gtgen.cli eval --run <run_id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

def load_env() -> None:
    """Load OPENROUTER_API_KEY etc. from the project's .env into os.environ (no overrides, never printed)."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gtgen.cli")


def _load_settings(path: str = "config/settings.yaml") -> dict:
    with open(path) as f:
        settings = yaml.safe_load(f)
    # Folder-based runs: the live_all_bids.sh wrapper exports these, and direct CLI users can too.
    paths = settings.setdefault("paths", {})
    for env_name, key in (
        ("GTGEN_RAW_DIR", "raw"),
        ("GTGEN_CANONICAL_DIR", "canonical"),
        ("GTGEN_OUT_DIR", "out"),
        ("GTGEN_LLM_CACHE", "llm_cache"),
    ):
        value = os.environ.get(env_name)
        if value:
            paths[key] = value
    return settings


def _settings_hash(settings: dict) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class PreparedBid:
    """Everything the dialogue needs that is shared by all personas of one bid."""
    bid_id: str
    scenario_id_base: str
    brief: "ScenarioBrief"             # post reframe + doc_classify; INCLUDES dropped constraints
    active_constraints: list           # brief.constraints minus framing_decision == DROP
    catalogue: "CatalogueView"
    product_category: str
    alignments: list                   # list[AttributeAlignment] over active_constraints
    qa_flags: list


def prepare_bid(bid_id: str, settings: dict, runners) -> PreparedBid:
    """Load a bid and run the shared LLM pre-dialogue steps: reframe -> doc_classify -> align.

    Inputs are reproduced exactly as record_pilot.py recorded them (no explicit seeds,
    most-common product_category) so mock-mode fixtures and the warm cache keep hitting.
    """
    import collections
    from gtgen.contracts import FramingDecision
    from gtgen.data.loaders import build_catalogue_view, build_scenario_brief

    raw_dir = settings["paths"]["raw"]
    scenario_id_base = bid_id.replace("/", "_")
    logger.info("Loading scenario brief for %s", bid_id)
    brief = build_scenario_brief(bid_id, raw_dir, settings, scenario_id=f"{scenario_id_base}_s0")
    logger.info("Loading catalogue view")
    catalogue = build_catalogue_view(bid_id, raw_dir, scenario_id=f"{scenario_id_base}_s0")
    logger.info("Catalogue: %d candidates", len(catalogue.candidates))

    # most-common category_name (record_pilot.py's computation), not the first candidate's
    counts = collections.Counter(c.category_name for c in catalogue.candidates if c.category_name)
    product_category = counts.most_common(1)[0][0] if counts else "General"

    # D054: live mode needs a ConversationContext on every call; mock keeps ctx=None so recorded fixtures/cache keys still match
    from gtgen.llm.context import ConversationContext
    ctx = (ConversationContext(scenario_id=f"{bid_id}:prep", cache_seed=0)
           if getattr(runners.client, "strict", False) else None)
    qa_flags: list[str] = []
    logger.info("Reframing %d constraints", len(brief.constraints))
    reframe_result = runners.reframe(product_category, brief.constraints, ctx=ctx)
    if not reframe_result.ok:
        logger.warning("Reframe had qa_flags: %s", reframe_result.qa_flags)
    qa_flags.extend(reframe_result.qa_flags)
    brief = brief.model_copy(update={"constraints": reframe_result.value})

    logger.info("Classifying doc-proof constraints")
    doc_result = runners.doc_classify(brief.constraints, ctx=ctx)
    if not doc_result.ok:
        logger.warning("Doc classify had qa_flags: %s", doc_result.qa_flags)
    qa_flags.extend(doc_result.qa_flags)
    brief = brief.model_copy(update={"constraints": doc_result.value})

    active = [c for c in brief.constraints if c.framing_decision != FramingDecision.DROP]
    logger.info("Active constraints after reframe: %d (dropped %d)",
                len(active), len(brief.constraints) - len(active))

    candidate_constraints = [cc for cv in catalogue.candidates for cc in cv.constraints]
    alignments: list = []
    if active and candidate_constraints:
        logger.info("Aligning %d buyer constraints x %d candidate constraints",
                    len(active), len(candidate_constraints))
        align_result = runners.align(bid_id, active, candidate_constraints, ctx=ctx)
        qa_flags.extend(align_result.qa_flags)
        alignments = align_result.value
    return PreparedBid(bid_id, scenario_id_base, brief, active, catalogue, product_category,
                       alignments, qa_flags)


# ---------------------------------------------------------------- pilot
def _resolve_bid(raw: str) -> str:
    """Accept a full id ("GEM/2019/B/231682") or a bare number ("231682", matched against the default bids)."""
    if "/" in raw:
        return raw
    from gtgen.data.scenario_table import DEFAULT_BIDS
    hits = [b for b in DEFAULT_BIDS if b.endswith("/" + raw)]
    if len(hits) != 1:
        raise SystemExit(f"cannot resolve bid {raw!r}; give the full id")
    return hits[0]


def cmd_pilot(args: argparse.Namespace) -> None:
    """Scenario pipeline (D046-D056): per (bid, persona seed) build the scenario, run the D056 loop, write GT."""
    if getattr(args, "data_dir", None):
        os.environ["GTGEN_RAW_DIR"] = args.data_dir
    settings = _load_settings()
    if args.live == args.mock:
        raise SystemExit("choose exactly one of --mock / --live")
    mode = "live" if args.live else "mock"
    os.environ["GTGEN_LLM_MODE"] = mode
    if args.fresh:
        os.environ["GTGEN_LLM_FRESH"] = "1"
    bids = [_resolve_bid(b) for b in args.bid]
    n_personas = args.personas
    if n_personas > 1:                 # D080 (user decision, 2026-11-01): at most one persona per bid
        raise SystemExit("pilot: at most one persona per bid is allowed (--personas must be 1); "
                         "pass --bid more than once for more bids")
    logger.info("Pilot: bids=%s, personas=%d, mode=%s, fresh=%s", bids, n_personas, mode, args.fresh)

    from gtgen.contracts import GTConversation
    from gtgen.data.scenario_build import build_scenario
    from gtgen.dialogue.assemble import append_conversation, assemble_conversation, conversation_path
    from gtgen.dialogue.loop import run_dialogue
    from gtgen.llm import LLMClient, LLMRunners, ScenarioSkipped

    run_id = f"pilot_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    # D086: create the run checkpoint before any LLM work.  Each finished conversation is appended and fsynced
    # immediately, so a timeout/crash keeps every completed bid/persona.
    out_dir = Path(settings["paths"]["out"]) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = conversation_path(run_id, settings)
    jsonl_path.touch(exist_ok=True)
    skipped_path = out_dir / "skipped.jsonl"
    (out_dir / "run_meta.json").write_text(json.dumps({
        "run_id": run_id,
        "bids": bids,
        "mode": mode,
        "fresh": bool(args.fresh),
        "personas": n_personas,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2) + "\n", encoding="utf-8")
    client = LLMClient(settings)
    runners = LLMRunners(client, settings)
    if hasattr(client, "set_run_id"):
        client.set_run_id(run_id)             # D073: every provenance event carries the run id

    # D066: no request cap. The gate only counts and logs progress; a failing FIRST live call still stops the run.
    from gtgen.llm.probe import HTTPGate
    gate = HTTPGate()
    gate.install()
    if mode == "live":
        import httpx
        from gtgen.llm.probe import FatalHTTP
        counted = httpx.post

        def first_call_guard(*a, **kw):
            if gate.count != 1:
                return counted(*a, **kw)
            first_call_backoff_s = [float(x) for x in settings["llm"]["http_retry"]["backoff_s"]]
            last = None
            for wait in [0.0] + first_call_backoff_s:
                if wait:
                    import time
                    time.sleep(wait)
                try:
                    r = counted(*a, **kw)
                except httpx.RequestError as exc:
                    import time
                    time.sleep(60)
                    last = None
                    continue
                if r.status_code == 200:
                    return r
                last = r
            if last is None:
                raise FatalHTTP("first live call never connected after retries (network/connection error)")
            raise FatalHTTP(f"first live call failed: HTTP {last.status_code}: {last.text[:800]}")
        httpx.post = first_call_guard

    n_scenarios = len(bids) * n_personas       # D060: strata are drawn over the whole run
    accepted_profiles: list = []
    scenario_index = 0
    conversations: list[GTConversation] = []
    skipped: list[str] = []
    skipped_rows: list[dict] = []
    stop_error: str | None = None
    try:
      for bid_id in bids:
        try:
            prepared = prepare_bid(bid_id, settings, runners)
        except ScenarioSkipped as e:         # D066: a failed prep step skips the whole bid, the run continues
            _record_skip(skipped, skipped_rows, bid_id, None, e, path=skipped_path)
            scenario_index += n_personas
            continue
        except FileNotFoundError as e:       # mock mode without a recorded fixture: never fabricate
            skipped.append(f"{bid_id}:prep:mock_fixture_missing")
            logger.warning("No mock fixture for prep of %s: %s", bid_id, str(e)[:200])
            scenario_index += n_personas
            continue
        for seed in range(1, n_personas + 1):
            idx, scenario_index = scenario_index, scenario_index + 1
            try:
                try:
                    built = build_scenario(bid_id, settings, runners, persona_seed=seed, prepared=prepared,
                                           run_key=run_id, n_scenarios=n_scenarios, scenario_index=idx,
                                           accepted_profiles=accepted_profiles)
                except FileNotFoundError as e:       # mock mode without a recorded fixture: never fabricate
                    skipped.append(f"{bid_id}:s{seed}:mock_fixture_missing")
                    logger.warning("No mock fixture for %s seed %d: %s", bid_id, seed, e)
                    continue
                if not built.ok:
                    skipped.append(f"{bid_id}:s{seed}:{','.join(built.qa_flags) or 'build_failed'}")
                    logger.error("Scenario build failed for %s seed %d: %s", bid_id, seed, built.qa_flags)
                    continue
                sk = built.skip_reason(settings) if hasattr(built, "skip_reason") else None
                if sk:                               # D074: invalid scenarios are skipped with the reason, never negotiated
                    skipped.append(f"{bid_id}:s{seed}:{sk}")
                    row = {"bid_id": bid_id, "persona_seed": seed, "prompt": None, "last_error": sk,
                          "scenario_meta": built.brief.scenario_meta}
                    skipped_rows.append(row)
                    _append_jsonl(skipped_path, row)
                    logger.warning("Scenario %s s%d skipped: %s", bid_id, seed, sk)
                    continue
                catalogue = prepared.catalogue.model_copy(update={"scenario_id": built.brief.scenario_id})
                state = run_dialogue(
                    brief=built.brief, persona=built.persona, buyer_profile=built.profile, catalogue=catalogue,
                    alignments=prepared.alignments, runners=runners, settings=settings,
                    product_category=prepared.product_category, doc_claims=built.doc_claims, scenario_mode=True,
                    sales_persona=built.brief.scenario_meta.get("sales_persona"),
                    claim_cache=getattr(built, "claim_cache", None),
                    normalised=getattr(built, "normalised", None) or None)   # truth-side evidence the build used   # D071: dialogue re-sends only pairs the table stage did not classify
                conv = assemble_conversation(state, run_id, settings)
                conv.qa_flags.extend(f for f in built.qa_flags if f not in conv.qa_flags)
                append_conversation(conv, run_id, settings)   # D086: checkpoint before the next bid
                conversations.append(conv)
            except ScenarioSkipped as e:     # D066: one bad scenario never takes down the batch
                _record_skip(skipped, skipped_rows, bid_id, seed, e, path=skipped_path)
                continue
            logger.info("%s s%d: %d turns, outcome=%s, qa_flags=%d", bid_id, seed, len(conv.turns),
                        conv.final.outcome, len(conv.qa_flags))
    except BaseException as exc:             # CapReached / FatalHTTP are BaseException: stop at once, keep what exists
        if type(exc).__name__ not in ("CapReached", "FatalHTTP"):
            raise
        stop_error = f"{type(exc).__name__}: {exc}"
        logger.error("STOPPED: %s", stop_error)

    # D086: conversations and skipped rows were appended as they finished; do not rewrite them here.
    if skipped_rows:                         # D066: skipped scenarios are listed, never silent
        print(f"Skipped scenarios written to {skipped_path}")
    prov = client.provenance_summary()
    print(f"\n{'=' * 60}\nPilot run: {run_id}\nBids: {bids}\nMode: {mode}  fresh: {args.fresh}\nOutput: {jsonl_path}")
    print(f"LLM calls: {prov}")
    if gate is not None:
        print(f"OBSERVED HTTP requests (incl. retries): {gate.count} | no cap (D066) | statuses: {dict(gate.status)}")
    print("=" * 60)
    for conv in conversations:
        print(f"\n{conv.scenario_id}: {conv.buyer_profile.company_name} tau={conv.persona.tau:.2f} "
              f"({conv.persona.tier.value}) turns={len(conv.turns)} outcome={conv.final.outcome} "
              f"top1_match={conv.top1_match_original} perfect={conv.top1_perfect_deal} "
              f"iterations={len(conv.relaxation_analysis)} proof_requests={len(conv.final.proof_requests)}")
        if conv.qa_flags:
            print(f"  QA flags: {conv.qa_flags[:6]}{'...' if len(conv.qa_flags) > 6 else ''}")
    for sk in skipped:
        print(f"SKIPPED {sk}")
    if stop_error:
        print(f"\nRUN STOPPED EARLY - exact error: {stop_error}")
    print()
    if stop_error:
        sys.exit(2)


def _append_jsonl(path: Path, row: dict) -> None:
    """Append one JSON line and fsync it (D086 checkpoint for skipped rows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _record_skip(skipped: list, rows: list, bid_id: str, seed, exc, path: Path | None = None) -> None:
    """D066: log one skipped scenario/bid clearly (bid, persona seed, failed prompt, last error)."""
    row = {"bid_id": bid_id, "persona_seed": seed, "prompt": getattr(exc, "prompt_name", None),
           "last_error": str(getattr(exc, "last_error", exc))[:500]}
    rows.append(row)
    if path is not None:
        _append_jsonl(path, row)
    skipped.append(f"{bid_id}:s{seed if seed is not None else 'prep'}:{row['prompt']}")
    logger.error("SKIPPED bid=%s seed=%s prompt=%s: %s", bid_id, seed, row["prompt"], row["last_error"])


# ---------------------------------------------------------------- eval
def cmd_eval(args: argparse.Namespace) -> None:
    """Evaluate a completed run."""
    settings = _load_settings()
    run_id = args.run
    out_dir = Path(settings["paths"]["out"]) / run_id

    jsonl_path = out_dir / "conversations.jsonl"
    if not jsonl_path.exists():
        print(f"ERROR: {jsonl_path} not found", file=sys.stderr)
        sys.exit(1)

    from gtgen.eval import persona_diversity, realism
    from gtgen.eval.conversations import evaluate_file, load_conversations

    convs = load_conversations(jsonl_path)
    logger.info("Loaded %d conversations from %s", len(convs), jsonl_path)
    result = evaluate_file(jsonl_path, settings)

    print(f"\nEvaluation: {run_id}\nConversations: {len(convs)}")
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    print("\n--- persona diversity (D060) ---")
    print(persona_diversity.render(persona_diversity.analyse_conversations(convs, settings)))
    print("\n--- realism, deterministic part (D061; budget check needs a catalogue) ---")
    print(realism.render(realism.analyse(convs, settings=settings)))


# ---------------------------------------------------------------- main
def main() -> None:
    parser = argparse.ArgumentParser(prog="gtgen", description="Negotiation GT Generator")
    sub = parser.add_subparsers(dest="command", required=True)

    # pilot
    p_pilot = sub.add_parser("pilot", help="Generate GT conversations for one bid")
    p_pilot.add_argument("--mock", action="store_true", help="Use mock LLM mode (no network)")
    p_pilot.add_argument("--live", action="store_true", help="Use live LLM mode")
    p_pilot.add_argument("--fresh", action="store_true", help="Ignore the LLM cache for reads (D054); every call is live")
    p_pilot.add_argument("--bid", required=True, nargs="+", help="Bid id(s), full (GEM/2019/B/231682) or bare number (231682)")
    p_pilot.add_argument("--data-dir", default=None,
                         help="Directory holding the raw CSVs (overrides paths.raw; same as GTGEN_RAW_DIR)")
    p_pilot.add_argument("--max-requests", type=int, default=None,
                         help="Ignored since D066 (no request cap); kept for old command lines")
    p_pilot.add_argument("--personas", type=int, default=1,
                         help="Personas per bid; D080 allows at most 1 (tau is derived from the bid itself)")
    p_pilot.set_defaults(func=cmd_pilot)

    # eval
    p_eval = sub.add_parser("eval", help="Evaluate a completed run")
    p_eval.add_argument("--run", required=True, help="Run ID to evaluate")
    p_eval.set_defaults(func=cmd_eval)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
