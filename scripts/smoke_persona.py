"""D062 smoke test: ONE live persona call for bid 488257, seed 1, on google/gemma-4-26b-a4b-it:free (google-ai-studio).

Hard-capped at --max-requests (default 5) HTTP requests, retries included. Any failing FIRST request stops the run and
prints the exact status and body (no retry, no provider fallback).
Prep (reframe/doc_classify/align) is read from the cache (non-fresh client); only the persona call is forced live.
Usage: GTGEN_LLM_MODE=live python scripts/smoke_persona.py [--max-requests 5] [--bid 488257] [--seed 1]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

os.environ.setdefault("GTGEN_LLM_MODE", "live")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-requests", type=int, default=5)
    ap.add_argument("--bid", default="488257")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()

    import httpx
    from gtgen.cli import _load_settings, _resolve_bid, prepare_bid
    from gtgen.data.scenario import commercial_bands, tightness_target
    from gtgen.llm import LLMClient, LLMRunners
    from gtgen.llm.context import ConversationContext
    from gtgen.llm.probe import FatalHTTP, HTTPGate
    from gtgen.llm.strata import persona_strata

    settings = _load_settings()
    bid = _resolve_bid(a.bid)
    gate = HTTPGate(a.max_requests)
    gate.install()
    counted = httpx.post
    raw: list[dict] = []

    def recorder(*args, **kw):
        n_before = gate.count
        r = counted(*args, **kw)
        body = None
        try:
            body = r.json()
        except Exception:
            pass
        raw.append({"request_no": gate.count, "status": r.status_code, "text": r.text, "json": body,
                    "request_body": kw.get("json")})
        if n_before == 0 and r.status_code != 200:
            raise FatalHTTP(f"first live call failed: HTTP {r.status_code}: {r.text[:800]}")
        return r

    httpx.post = recorder

    # prep: cache-backed (non-fresh); must cost 0 HTTP requests
    os.environ.pop("GTGEN_LLM_FRESH", None)
    os.environ["GTGEN_LLM_MODE"] = "mock"          # cache first, then fixtures; never the network
    prep_runners = LLMRunners(LLMClient(settings), settings)
    prepared = prepare_bid(bid, settings, prep_runners)
    print(f"[smoke] prep done, HTTP requests so far: {gate.count}", file=sys.stderr)

    # persona: forced live
    os.environ["GTGEN_LLM_MODE"] = "live"
    client = LLMClient(settings, fresh=True)
    runners = LLMRunners(client, settings)
    brief0 = prepared.brief
    quantity = float(brief0.quantity)
    bands = commercial_bands(prepared.catalogue, quantity, settings)
    cats = [c.constraint_category for c in prepared.active_constraints] + \
           [settings["scenario"]["commercial_terms"]["category"][t] for t in bands["terms"]]
    strata = persona_strata(a.seed, 1, "smoke", settings, cats, index=0)
    ctx = ConversationContext(scenario_id=f"{bid}:s{a.seed}", persona_seed=a.seed)
    err = None
    res = None
    try:
        res = runners.persona(prepared.product_category, quantity, brief0.quantity_unit, None, cats, a.seed,
                              f"{bid}:s{a.seed}", bands=bands, tightness_target=tightness_target(a.seed, settings),
                              ctx=ctx, strata=strata)
    except BaseException as exc:      # CapReached / FatalHTTP / budget errors: report exactly
        err = f"{type(exc).__name__}: {exc}"

    print("=" * 70)
    print(f"model={settings['llm']['model']} provider_order={settings['llm']['backends'][settings['llm']['backend']]['provider']}")
    print(f"HTTP requests observed: {gate.count} (cap {gate.cap}); statuses: {dict(gate.status)}")
    for r in raw:
        print(f"\n--- raw response of HTTP request #{r['request_no']} (status {r['status']}) ---")
        print(r["text"][:6000])
        j = r["json"] or {}
        print(f"provider used by OpenRouter: {j.get('provider')!r}   model: {j.get('model')!r}")
        print(f"usage: {json.dumps(j.get('usage'))}")
        print(f"response_format sent: {json.dumps((r['request_body'] or {}).get('response_format'))[:300]}")
    if err:
        print(f"\nCALL RAISED: {err}")
    for ev in client.failed_events:
        print("\nFAILED EVENT:", json.dumps(ev, default=str)[:1500])
    for ev in client.events:
        print("\nPROVENANCE EVENT:", json.dumps(ev, default=str)[:2500])
    if res is not None:
        print(f"\nqa_flags: {res.qa_flags}  ok={res.ok}")
        if res.value is not None:
            prof, pers = res.value
            print("BuyerProfile:", prof.model_dump_json(indent=1))
            print("Persona:", pers.model_dump_json(indent=1))
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
