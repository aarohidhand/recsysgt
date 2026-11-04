"""Live probe: how much real evidence does ``normalise`` recover from candidates' free text?

    GTGEN_LLM_MODE=live python -m gtgen.llm.probe run --in out/coverage_llm

Reads ``*_requests.json`` (written by data/coverage_llm.py), calls ``LLMRunners.normalise`` in
chunks of ``llm.batch_max_constraints``, checks every non-null value for grounding in its snippet
(CLAUDE.md rule 1.8: an ungrounded value is never evidence) and writes ``<slug>_normalised.json``.
There is no request cap (D066); the gate only counts.
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import random
import re
import sys
from pathlib import Path
from typing import Any, Optional

from .context import ConversationContext
from .exceptions import ScenarioSkipped

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_NUM_RE = re.compile(_NUM)
_RANGE_SEP = r"(?:\.\.|-|–|—|\bto\b)"
_NUMERIC_VALUE = re.compile(
    rf"^\s*[-+]?(?P<a>{_NUM})(?:\s*{_RANGE_SEP}\s*[-+]?(?P<b>{_NUM}))?\s*[A-Za-z%°/²³.\s]{{0,16}}$")
FATAL_STATUS = (401, 402, 403)
logger = logging.getLogger(__name__)


class CapReached(BaseException):
    """Kept importable for compatibility; D066 removed the request cap, so nothing raises it any more."""


class FatalHTTP(BaseException):
    """Authentication / payment failure: stop immediately, no retries."""


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(abs(a), abs(b), 1e-12)


def is_grounded(value: Optional[str], snippet: str) -> bool:
    """Numeric values: every number/range bound must occur numerically in the snippet.
    Otherwise: the lowercased, whitespace-collapsed value must be a substring of the snippet."""
    if value is None or not str(value).strip():
        return False
    value, snippet = str(value), snippet or ""
    m = _NUMERIC_VALUE.match(value)
    if m:
        bounds = [_to_float(g) for g in (m.group("a"), m.group("b")) if g]
        found = [_to_float(x) for x in _NUM_RE.findall(snippet)]
        return all(any(_close(b, f) for f in found) for b in bounds)
    norm = lambda s: " ".join(s.lower().split())  # noqa: E731
    return norm(value) in norm(snippet)


class HTTPGate:
    """Counts every HTTP attempt and logs progress every ``LOG_EVERY`` requests (D066: no hard cap, never stops a run).

    ``cap`` is accepted for backward compatibility and ignored.  ``CapReached`` is no longer raised by anything.
    """
    LOG_EVERY = 10

    def __init__(self, cap: Optional[int] = None) -> None:
        self.cap, self.count, self.status = cap, 0, collections.Counter()

    def request(self) -> None:
        self.count += 1
        if self.count % self.LOG_EVERY == 0:
            logger.info("HTTP requests so far: %d", self.count)

    def install(self) -> None:
        import httpx
        real = httpx.post

        def counted(*a: Any, **kw: Any):
            self.request()
            try:
                r = real(*a, **kw)
            except Exception as exc:
                self.status[type(exc).__name__] += 1
                raise
            self.status[r.status_code] += 1
            print(f"[probe] http #{self.count} status={r.status_code}", file=sys.stderr, flush=True)
            if r.status_code in FATAL_STATUS:
                raise FatalHTTP(f"HTTP {r.status_code} (auth/quota); stopping")
            return r

        httpx.post = counted


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def run(in_dir: str | Path, runners: Any, gate: HTTPGate, batch: int = 80,
        sample_seed: int = 7) -> dict[str, dict]:
    """Process all request files; always writes outputs. Returns {slug: output}."""
    from gtgen.llm import NormaliseRequest

    in_dir = Path(in_dir)
    stop: Optional[str] = None
    outputs: dict[str, dict] = {}
    for f in sorted(in_dir.glob("*_requests.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        bid, seed, reqs = data["bid"], data.get("seed"), data["requests"]
        slug = bid.replace("/", "_")
        results: dict[str, dict] = {}
        meta: dict[str, Any] = {"normalise_calls": 0, "http_requests": 0, "qa_flags": [],
                                "incomplete": False, "unprocessed": [], "samples": []}
        start_http = gate.count
        for ci, chunk in enumerate(_chunks(reqs, batch)):
            rids = [r["rid"] for r in chunk]
            if stop:
                meta["unprocessed"] += rids
                continue
            nreqs = [NormaliseRequest(r["text"], r["target"], r["canonical_unit"]) for r in chunk]
            try:
                res = runners.normalise(nreqs, seed, ctx=ConversationContext(f"probe:{bid}", int(seed or 0)))
            except FatalHTTP as exc:
                stop = str(exc)
                meta["unprocessed"] += rids
                meta["qa_flags"].append(f"stopped:{stop}")
                continue
            except ScenarioSkipped as exc:   # retries exhausted: this chunk stays absent, the run goes on
                meta["qa_flags"].append(f"skipped:{exc}")
                continue
            meta["normalise_calls"] += len(res.llm_call_ids)
            meta["qa_flags"] += list(res.qa_flags)
            if res.qa_flags:
                continue  # failed chunk: items absent, never invented
            cid = res.llm_call_ids[-1] if res.llm_call_ids else ""
            for r, v in zip(chunk, res.value):
                if v is None:
                    results[r["rid"]] = {"value": None, "unit": None, "confidence": None,
                                         "llm_call_id": cid, "grounded": False}
                else:
                    results[r["rid"]] = {"value": v.value, "unit": v.unit, "confidence": v.confidence,
                                         "llm_call_id": v.llm_call_id or cid,
                                         "grounded": is_grounded(v.value, r["text"])}
        text_by_rid = {r["rid"]: r["text"] for r in reqs}
        nonnull = sorted(k for k, v in results.items() if v["value"] is not None)
        picks = random.Random(sample_seed).sample(nonnull, min(10, len(nonnull)))
        meta["samples"] = [{"rid": k, "text": text_by_rid[k], "value": results[k]["value"],
                            "unit": results[k]["unit"], "grounded": results[k]["grounded"]} for k in sorted(picks)]
        meta["http_requests"] = gate.count - start_http
        meta["incomplete"] = bool(meta["unprocessed"])
        out = {"bid": bid, "results": results, "meta": meta}
        (in_dir / f"{slug}_normalised.json").write_text(
            json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
        outputs[slug] = out
    return outputs


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="gtgen.llm.probe")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--in", dest="in_dir", default="out/coverage_llm")
    p.add_argument("--cap", type=int, default=None, help="ignored (D066: no request cap)")
    p.add_argument("--settings", default="config/settings.yaml")
    a = ap.parse_args(argv)

    from gtgen.cli import load_env
    from gtgen.data.loaders import load_settings
    from gtgen.llm import LLMClient, LLMRunners
    load_env()
    settings = load_settings(a.settings)
    gate = HTTPGate()
    gate.install()
    runners = LLMRunners(LLMClient(settings), settings)
    outs = run(a.in_dir, runners, gate, batch=int(settings["llm"]["batch_max_constraints"]))
    incomplete = any(o["meta"]["incomplete"] for o in outs.values())
    print(f"HTTP requests: {gate.count} ; status counts: {dict(gate.status)}", file=sys.stderr)
    for slug, o in outs.items():
        m = o["meta"]
        print(slug, "calls", m["normalise_calls"], "http", m["http_requests"], "incomplete", m["incomplete"],
              "qa_flags", len(m["qa_flags"]))
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
