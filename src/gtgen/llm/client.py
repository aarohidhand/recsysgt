"""OpenRouter LLM client with disk cache, mock, and record modes.

Modes (env var ``GTGEN_LLM_MODE``):
  live    — call OpenRouter, cache on disk (default).
  mock    — load responses from ``tests/fixtures/llm/``, no network.
  record  — call OpenRouter, cache on disk *and* write to fixtures.
"""
from __future__ import annotations

import hashlib
import json
import math
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .exceptions import LLMBudgetError, LLMNetworkError, LLMValidationError

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)

_VALID_MODES = {"live", "mock", "record"}
_STRUCTURED_MODES = ("auto", "json_schema", "prompt_json")



class RateLimiter:
    """Process-wide, thread-safe pacing of outgoing HTTP requests (D024).

    ``acquire(min_interval_s)`` reserves the next request slot under a lock and
    then sleeps (outside the lock) until that slot, so the *starts* of any two
    requests are >= ``min_interval_s`` apart, whichever thread or LLMClient
    issues them.  ``clock`` and ``sleep`` are injectable for tests.
    """

    def __init__(self, clock=time.monotonic, sleep=time.sleep) -> None:
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._next_slot: float | None = None
        self._tok_lock = threading.Lock()
        self._window: list[tuple[float, int]] = []     # D068: (timestamp, estimated tokens) of the last 60 s

    def reserve_tokens(self, est: int, budget: float, label: str = "", window_s: float = 60.0) -> float:
        """D068 token pacer: block until ``est`` tokens fit into the rolling ``window_s`` window (sum <= ``budget``),
        then record the reservation.  A request whose ``est`` alone exceeds the budget waits for an EMPTY window and
        goes alone (WARNING) instead of deadlocking.  Returns seconds waited.  Thread-safe; the wait is computed
        exactly from the expiry times of the oldest entries."""
        waited = 0.0
        oversize_logged = False
        while True:
            with self._tok_lock:
                now = self.clock()
                self._window = [(t, n) for t, n in self._window if now - t < window_s]
                total = sum(n for _, n in self._window)
                if est > budget and not oversize_logged:
                    logger.warning("token pacer: single call of ~%d est. tokens exceeds the budget %d for key %s; "
                                   "sending it alone (batching should prevent this)", est, int(budget), label)
                    oversize_logged = True
                limit = max(budget, est)      # oversize: needs an empty window (total must be 0)
                if est > budget:
                    ok = total == 0
                else:
                    ok = total + est <= limit
                if ok:
                    self._window.append((now, est))
                    return waited
                need = (total + est - budget) if est <= budget else total
                freed, wait_until = 0, now
                for t, n in sorted(self._window):
                    freed += n
                    wait_until = t + window_s
                    if freed >= need:
                        break
                wait = max(wait_until - now, 0.0)
            logger.info("token pacer: waiting %.1fs for key %s", wait, label)
            self.sleep(wait)
            waited += wait

    def acquire(self, min_interval_s: float) -> float:
        """Block until a request may start. Returns seconds waited."""
        with self._lock:
            now = self.clock()
            slot = now if self._next_slot is None else max(now, self._next_slot)
            self._next_slot = slot + min_interval_s
        wait = slot - now
        if wait > 0:
            self.sleep(wait)
        return max(wait, 0.0)

    def reset(self) -> None:
        with self._lock:
            self._next_slot = None
        with self._tok_lock:
            self._window = []


# D062: the identity under which the checked-in mock fixtures were recorded (live, the :free model routed to
# google-ai-studio, D022) or synthesised (tests/fixtures/llm/PROVENANCE.md).  It equals the configured model and
# provider, so mock mode needs no fallback lookup (D062 removed it).  Used by synth_fixtures and tests; the disk cache (.cache/llm) is never read under it in live/record mode.
FIXTURE_MODEL = "google/gemma-4-26b-a4b-it:free"
FIXTURE_BACKEND = "openrouter"
FIXTURE_PROVIDER: dict = {"order": ["google-ai-studio"], "allow_fallbacks": False}


class LimiterPool:
    """One ``RateLimiter`` per API-key VARIABLE name (D063): separate accounts have independent limits,
    two roles that resolve to the same variable share one limiter.  ``clock`` and ``sleep`` are injectable
    (set on the pool; they are propagated to every limiter on access)."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep) -> None:
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._limiters: dict[str, RateLimiter] = {}

    def get(self, key_env: str) -> RateLimiter:
        with self._lock:
            lim = self._limiters.get(key_env)
            if lim is None:
                lim = self._limiters[key_env] = RateLimiter()
            lim.clock, lim.sleep = self.clock, self.sleep
            return lim

    def reset(self) -> None:
        with self._lock:
            self._limiters.clear()


# Shared by every LLMClient instance in the process; keyed by API-key variable name (D063).
_LIMITER = LimiterPool()

# D063: the ONE place that maps prompt name -> API-key role.  The role -> environment variable map lives in
# config/settings.yaml (llm.api_key_roles).  Every name in prompts.PROMPT_NAMES must resolve (tests enforce it);
# negotiator_* prompts are matched by prefix so a new negotiator prompt cannot fall through to another role.
PROMPT_ROLES: dict[str, str] = {
    "reframe": "prep", "doc_classify": "prep", "align": "prep", "normalise": "prep",
    "doc_claim": "claims", "claim_check": "claims",
    "persona": "persona",
    "buyer_opening": "buyer", "buyer_reply": "buyer", "buyer_reveal": "buyer", "reveal_check": "buyer",
}
API_KEY_ROLES = ("prep", "claims", "persona", "buyer", "negotiator")
_PLACEHOLDER_MARK = "REPLACE_ME"


def role_for_prompt(prompt_name: str) -> str | None:
    """API-key role of a prompt, or None if unmapped (callers then use the default key)."""
    if prompt_name.startswith("negotiator_"):
        return "negotiator"
    return PROMPT_ROLES.get(prompt_name)


def _usable_key(value: str | None) -> bool:
    return bool(value and value.strip() and _PLACEHOLDER_MARK not in value)


def strip_code_fence(text: str) -> str:
    """Strip exactly one surrounding markdown code fence (``` or ```json).

    Nothing else is done: no JSON extraction from prose.  Text that is not
    wholly wrapped in a single fence is returned unchanged.
    """
    t = text.strip()
    if t.startswith("```") and t.endswith("```") and len(t) >= 6:
        inner = t[3:-3]
        nl = inner.find("\n")
        if nl == -1:
            return inner.strip()  # e.g. ```{"a":1}```
        first = inner[:nl].strip()
        if first == "" or first.isalnum():  # language tag such as "json"
            return inner[nl + 1:].strip()
        return inner.strip()
    return text


def repair_trailing_commas(text: str) -> str:
    """Remove a comma that is followed (after whitespace) by } or ], outside string literals only (D068)."""
    out: list[str] = []
    in_str = esc = False
    n = len(text)
    for i, ch in enumerate(text):
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                continue
        out.append(ch)
    return "".join(out)


def tolerant_json_text(text: str) -> str:
    """``text`` if it parses as JSON; else the trailing-comma-repaired text if THAT parses; else ``text`` unchanged
    (the schema validation then reports the original error).  Only valid JSON is ever accepted."""
    try:
        json.loads(text)
        return text
    except ValueError:
        pass
    fixed = repair_trailing_commas(text)
    if fixed != text:
        try:
            json.loads(fixed)
            return fixed
        except ValueError:
            pass
    return text


_JSON_RATIO = 3.0     # chars per token for JSON-heavy text (many quotes, braces, digits, short words)
_PROSE_RATIO = 3.5    # chars per token for plain English prose
_JSON_PUNCT_SHARE = 0.12
_MSG_OVERHEAD = 4     # per message: role + separators
_REPLY_PRIMING = 3    # assistant-turn priming


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (D062); Gemma's tokenizer is not available offline.

    Plain English averages ~4 chars/token, but the prompts here are JSON-heavy (quotes, braces, digits,
    unit strings) which tokenize at ~2.5-3.3 chars/token, and non-ASCII text (Devanagari, symbols) is
    close to one token per character.  So: ASCII text with >=12% punctuation (JSON-ish) uses chars/3,
    other ASCII text chars/3.5, each non-ASCII character counts as one token.  This deliberately
    over-estimates (a few %-points to ~30%) so a call never exceeds the endpoint's input+output token limit; the
    real ``usage`` is logged next to the estimate in provenance so the error is visible.
    """
    n = len(text)
    if n == 0:
        return 0
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    ascii_n = n - non_ascii
    punct = sum(1 for ch in text if ord(ch) <= 127 and not (ch.isalnum() or ch.isspace()))
    ratio = _JSON_RATIO if ascii_n and punct / ascii_n >= _JSON_PUNCT_SHARE else _PROSE_RATIO
    return math.ceil(ascii_n / ratio) + non_ascii


def estimate_messages_tokens(messages: list[dict[str, str]], extra_text: str = "") -> int:
    """Estimated input tokens of a chat request: all contents + per-message overhead + priming (+ ``extra_text``)."""
    total = sum(estimate_tokens(m.get("content", "")) + _MSG_OVERHEAD for m in messages) + _REPLY_PRIMING
    return total + (estimate_tokens(extra_text) if extra_text else 0)


def schema_text(response_model: Type[BaseModel]) -> str:
    return json.dumps(response_model.model_json_schema(), separators=(",", ":"), sort_keys=True)


def extract_json(text: str) -> str:
    """prompt_json mode: strip one code fence; if the text is still not a JSON object, take first '{' .. last '}'."""
    t = strip_code_fence(text).strip()
    if t.startswith("{") and t.endswith("}"):
        return t
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if 0 <= i < j else t


def first_json_object(text: str) -> tuple[str, str] | None:
    """Return ``(object_text, trailing_text)`` when *text* starts with a complete JSON object.

    A model sometimes appends reasoning after a valid JSON object even when
    ``response_format`` is set.  We accept that leading object (schema validation still
    runs), but deliberately do **not** extract an object that follows prose: in
    structured-output mode the reply must start with JSON (D024).  One leading markdown
    fence is tolerated because some providers still wrap the answer.
    """
    t = strip_code_fence(text).strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:].strip()
    if not t.startswith("{"):
        return None
    try:
        _, end = json.JSONDecoder().raw_decode(t)
    except ValueError:
        return None
    return t[:end], t[end:].strip()


_RETRY_IN_RE = re.compile(r"retry\s+in\s+([0-9]+(?:\.[0-9]+)?)\s*s", re.I)
_DELAY_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*s?\s*$", re.I)


def _hints_from_obj(obj: Any, out: list[float], depth: int = 0) -> None:
    """Collect provider retry hints (seconds) from a decoded error body; recurses into metadata.raw."""
    if depth > 4:
        return
    if isinstance(obj, list):
        for it in obj:
            _hints_from_obj(it, out, depth + 1)
        return
    if not isinstance(obj, dict):
        return
    err = obj.get("error", obj)
    if not isinstance(err, dict):
        if isinstance(err, str):
            _hints_from_text(err, out)
        return
    details = err.get("details")
    if isinstance(details, list):
        for d in details:
            if isinstance(d, dict) and d.get("retryDelay") is not None:
                m = _DELAY_RE.match(str(d["retryDelay"]))
                if m:
                    out.append(float(m.group(1)))
    msg = err.get("message")
    if isinstance(msg, str):
        for m in _RETRY_IN_RE.finditer(msg):
            out.append(float(m.group(1)))
    meta = err.get("metadata")
    if isinstance(meta, dict):
        raw = meta.get("raw")
        if isinstance(raw, str):
            _hints_from_text(raw, out, depth + 1)
        elif raw is not None:
            _hints_from_obj(raw, out, depth + 1)


def _hints_from_text(text: str, out: list[float], depth: int = 0) -> None:
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        decoded = None
    if isinstance(decoded, (dict, list)):
        _hints_from_obj(decoded, out, depth + 1)
    else:   # non-JSON text: regex it
        for m in _RETRY_IN_RE.finditer(text):
            out.append(float(m.group(1)))


def body_retry_hint(text: str) -> float | None:
    """Retry hint (seconds) from an error response body (OpenRouter-wrapped or plain); None if none found."""
    out: list[float] = []
    _hints_from_text(text or "", out)
    return max(out) if out else None


def _gemini_body(messages: list[dict[str, str]], temperature: float | None, max_tokens: int,
                 disable_reasoning: bool) -> dict:
    """OpenAI-style messages -> Gemini generateContent body (system -> systemInstruction, assistant -> model).
    shortcut: seed is not sent (the Gemma on Gemini API docs do not list it); it stays in the cache key."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    body: dict[str, Any] = {"contents": [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in messages if m["role"] != "system"]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    config: dict[str, Any] = {"maxOutputTokens": max_tokens}
    if temperature is not None:
        config["temperature"] = temperature
    if disable_reasoning:
        # Gemma 4: "minimal" turns thinking off. Source: https://ai.google.dev/gemma/docs/core/gemma_on_gemini_api
        config["thinkingConfig"] = {"thinkingLevel": "minimal"}
    body["generationConfig"] = config
    return body


def _from_gemini(data: dict) -> dict:
    """Gemini generateContent response -> the OpenAI chat shape the client reads; "thought" parts are dropped."""
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    usage = data.get("usageMetadata") or {}
    return {
        "choices": [{"message": {"content": "".join(p.get("text", "") for p in parts if not p.get("thought"))},
                     "finish_reason": cand.get("finishReason")}],
        "usage": {"prompt_tokens": usage.get("promptTokenCount", 0),
                  "completion_tokens": usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
                  "total_tokens": usage.get("totalTokenCount", 0)},
        "model": data.get("modelVersion"),
        "error": data.get("promptFeedback"),     # blockReason etc.; only used in the empty-answer diagnostic
    }


def compute_wait_s(linear_s: float, hint_s: float | None, margin_s: float, max_wait_s: float) -> float:
    """D067: max(linear, int(hint + margin)) clamped to [8, max_wait_s]."""
    wait = float(linear_s)
    if hint_s is not None:
        wait = max(wait, float(int(hint_s + margin_s)))
    return min(max(wait, 8.0), float(max_wait_s))


def _require(cfg: dict, key: str, where: str = "llm") -> Any:
    if key not in cfg or cfg[key] is None:
        raise ValueError(f"Missing required setting {where}.{key} (see D024)")
    return cfg[key]


def active_llm(settings: dict) -> tuple[str, str]:
    """(backend, model) a client built from *settings* really uses: GTGEN_LLM_BACKEND overrides llm.backend and the
    backend block's model overrides llm.model.  Provenance records these (D108), not the settings-file defaults."""
    llm_cfg = settings.get("llm", {})
    backend = os.environ.get("GTGEN_LLM_BACKEND") or llm_cfg.get("backend", "openrouter")
    b = (llm_cfg.get("backends") or {}).get(backend) or {}
    return backend, b.get("model", llm_cfg.get("model", "google/gemma-4-26b-a4b-it:free"))


class LLMClient:
    """OpenRouter LLM client with disk cache, mock, and record modes."""

    def __init__(self, settings: dict, cache_dir: str | None = None, fresh: bool | None = None) -> None:
        llm_cfg = settings.get("llm", {})
        self._model: str = llm_cfg.get("model", "google/gemma-4-26b-a4b-it:free")
        self._base_url: str = llm_cfg.get("base_url", "https://openrouter.ai/api/v1")
        self._api_key_env: str = llm_cfg.get("api_key_env", "OPENROUTER_API_KEY")
        self._api_key_roles: dict[str, str] = dict(llm_cfg.get("api_key_roles") or {})   # D063
        # D084: optional per-role key pools.  Calls round-robin over the usable keys; because the limiter is
        # keyed by env-var name, every pooled account keeps its own token pacer.
        raw_pools = llm_cfg.get("api_key_pools") or {}
        self._api_key_pools: dict[str, list[str]] = {}
        for role, names in raw_pools.items():
            if names is None:                    # `claims:` with no value means "no pool"
                continue
            if isinstance(names, str):
                names = [names]
            self._api_key_pools[str(role)] = [str(name) for name in names]
        self._key_cursor: dict[str, int] = {}
        self._key_lock = threading.Lock()
        self._prompt_name: str | None = None      # set by call(); selects the key role for _api_call
        self._last_key: dict = {}                 # {"api_key_role", "api_key_env"} of the latest request
        backend, model = active_llm(settings)
        backends: dict = llm_cfg.get("backends", {})
        if backend not in backends:
            raise ValueError(
                f"Unknown LLM backend {backend!r}; configured: {sorted(backends)}"
            )
        self._backend: str = backend
        b = backends[backend]
        self._provider: dict = dict(b.get("provider", {}))
        # Direct Google backend (api: gemini): native generateContent, one key for every role (no OpenRouter pools).
        self._gemini: bool = b.get("api") == "gemini"
        self._base_url = b.get("base_url", self._base_url)
        self._model = model
        self._extra_body: dict = dict(b.get("extra_body") or {})   # D108: backend-specific body fields (OpenAI path)
        if b.get("api_key_env"):
            self._api_key_env, self._api_key_roles, self._api_key_pools = b["api_key_env"], {}, {}
        # D024 runtime limits: required, no silent defaults.  They are NOT part
        # of the cache key (D018).
        self._max_tokens_per_call: int = int(_require(llm_cfg, "max_tokens_per_call"))
        total = llm_cfg.get("token_limit_total")      # D062: hard endpoint limit (input + output)
        if total is not None and self._max_tokens_per_call > int(total):
            raise ValueError(f"llm.max_tokens_per_call={self._max_tokens_per_call} exceeds llm.token_limit_total={total}")
        self._max_tokens: int = int(_require(llm_cfg, "max_completion_tokens"))
        self._min_completion: int = int(_require(llm_cfg, "min_completion_tokens"))
        self._max_retries: int = int(_require(llm_cfg, "max_retries"))
        self._timeout_s: float = float(_require(llm_cfg, "timeout_s"))
        # D108: a backend block may set its own limits (another provider's quota); tpm_limit: null = pacer off.
        rl = b.get("rate_limit") or _require(llm_cfg, "rate_limit")
        tpm = b.get("tpm_limit", llm_cfg.get("tpm_limit"))   # D068: absent = token pacer off
        self._tpm_budget: float | None = (float(tpm) * float(llm_cfg.get("tpm_safety", 0.8))) if tpm else None
        self._batch_completion_target: int = int(llm_cfg.get("batch_completion_target", self._max_tokens))
        self._expected_completion: int | None = None
        self._min_interval_s: float = float(_require(rl, "min_interval_s", "llm.rate_limit"))
        hr = _require(llm_cfg, "http_retry")
        self._http_max_attempts: int = int(_require(hr, "max_attempts", "llm.http_retry"))
        self._backoff_s: list[float] = [float(x) for x in _require(hr, "backoff_s", "llm.http_retry")]
        if not self._backoff_s:
            raise ValueError("llm.http_retry.backoff_s must not be empty")
        self._respect_retry_after: bool = bool(_require(hr, "respect_retry_after", "llm.http_retry"))
        self._max_retry_after_s: float = float(_require(hr, "max_retry_after_s", "llm.http_retry"))
        self._margin_s: float = float(hr.get("retry_after_margin_s", 5))
        self._max_wait_s: float = float(hr.get("max_wait_s", 300))
        self._non_retryable: set[int] = {int(x) for x in hr.get("non_retryable_statuses", [400, 401, 402, 403, 404, 422])}
        # transport only, not in cache key; D108: a backend block may override it (its extra_body says how instead)
        self._disable_reasoning: bool = bool(b.get("disable_reasoning", llm_cfg.get("disable_reasoning", False)))
        self._temperatures: dict[str, float] = llm_cfg.get("temperature", {})
        # D062: structured-output mode; absent = auto (current behaviour: send response_format json_schema).
        self._structured: str = str(b.get("structured_output", llm_cfg.get("structured_output", "auto")))
        if self._structured not in _STRUCTURED_MODES:
            raise ValueError(f"llm.structured_output must be one of {_STRUCTURED_MODES}, got {self._structured!r}")
        self._empty_diag: str | None = None
        self._reset_call_state()

        # Cache directory
        if cache_dir is not None:
            self._cache_dir = Path(cache_dir)
        else:
            self._cache_dir = Path(
                settings.get("paths", {}).get("llm_cache", ".cache/llm")
            )

        # Mode
        mode = os.environ.get("GTGEN_LLM_MODE", "live").lower()
        if mode not in _VALID_MODES:
            raise ValueError(
                f"GTGEN_LLM_MODE must be one of {_VALID_MODES}, got {mode!r}"
            )
        self._mode = mode

        # Fixture directory (for mock and record modes)
        self._fixture_dir = Path("tests/fixtures/llm")

        # D054: ``fresh`` = never read the disk cache (every call goes live / to its fixture).
        if fresh is None:
            fresh = os.environ.get("GTGEN_LLM_FRESH", "").strip().lower() in {"1", "true", "yes"}
        self._fresh = bool(fresh)
        # D054 provenance: one event per call() — source is live | cache_hit | fixture.
        self.events: list[dict] = []
        self.prep_events: list[dict] = []     # D070: prep_paths events (see record_prep_paths)
        # D073: run key for provenance linkage (not part of the cache key): GTGEN_RUN_ID or set via ``set_run_id``;
        # a ``run_id`` inside a call's context dict (ConversationContext.run_id) takes precedence.
        self.run_id: str | None = os.environ.get("GTGEN_RUN_ID") or None

    # -------------------------------------------------------------- mode / provenance (D054)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def fresh(self) -> bool:
        return self._fresh

    @property
    def is_live(self) -> bool:
        """True when responses may only come from the model (live or record): no fixtures, no stubs."""
        return self._mode in ("live", "record")

    @property
    def strict(self) -> bool:
        """True only in live mode: runners then demand ConversationContext, persona, sales persona and bands (D054).
        Record mode is the fixture recorder and stays lenient."""
        return self._mode == "live"

    def set_run_id(self, run_id: str | None) -> None:
        self.run_id = run_id or None

    def _run_id_for(self, context: dict | None) -> str | None:
        return (context or {}).get("run_id") or self.run_id

    def _reset_call_state(self) -> None:
        self._http_attempts = 0
        self._requests: list[dict] = []       # one record per outgoing HTTP request (est. tokens, max_tokens, usage)
        self._so_result: dict | None = None
        self.failed_events: list[dict] = []   # live calls that raised (e.g. HTTP 400 on response_format)

    def _log_event(self, key: str, prompt_name: str, prompt_version: int, source: str,
                   context: dict | None, http_attempts: int = 0, extra: dict | None = None) -> None:
        ev = {
            "llm_call_id": key, "prompt_name": prompt_name, "prompt_version": prompt_version,
            "source": source, "http_attempts": http_attempts,
            "scenario_id": (context or {}).get("scenario_id"), "turn_idx": (context or {}).get("turn_idx"),
            "persona_seed": (context or {}).get("persona_seed"), "run_id": self._run_id_for(context),
        }
        ev.update(extra or {})
        ev["api_key_role"] = self._last_key.get("api_key_role", role_for_prompt(prompt_name))
        if "api_key_env" in self._last_key:
            ev["api_key_env"] = self._last_key["api_key_env"]      # variable NAME only, never a value
        (self.failed_events if source == "failed" else self.events).append(ev)
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            with open(self._cache_dir / "provenance.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, sort_keys=True) + "\n")
        except OSError:  # provenance logging must never break a call
            logger.warning("could not append provenance log")

    def record_prep_paths(self, prompt_name: str, paths: dict, item_paths: dict, context: dict | None = None) -> None:
        """D066/D069/D070: persist how each item of a deterministic-first prep runner was resolved
        (path = rule | llm | prefilter | cache) into the provenance log, one line per runner invocation:
        ``{"event": "prep_paths", "prompt_name", "paths": {path: n}, "item_paths": {item: path}, scenario_id, ...}``.
        Kept apart from ``events`` (those are per-call) in ``prep_events``."""
        ev = {"event": "prep_paths", "prompt_name": prompt_name, "paths": dict(paths), "item_paths": dict(item_paths),
              "n_items": len(item_paths), "scenario_id": (context or {}).get("scenario_id"),
              "turn_idx": (context or {}).get("turn_idx"), "persona_seed": (context or {}).get("persona_seed"),
              "run_id": self._run_id_for(context)}
        self.prep_events.append(ev)
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            with open(self._cache_dir / "provenance.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, sort_keys=True) + "\n")
        except OSError:
            logger.warning("could not append provenance log")

    def prep_path_summary(self, scenario_id: str | None = None) -> dict:
        """Sum of ``paths`` over the recorded prep events (optionally one scenario): {"rule": n, "llm": n, ...}."""
        out: dict[str, int] = {}
        for e in self.prep_events:
            if scenario_id is None or e.get("scenario_id") == scenario_id:
                for k, v in e["paths"].items():
                    out[k] = out.get(k, 0) + int(v)
        return out

    def provenance_summary(self, scenario_id: str | None = None) -> dict:
        """Counts per source and ``live_share`` (live / all events) for this client, optionally one scenario."""
        evs = [e for e in self.events if scenario_id is None or e["scenario_id"] == scenario_id]
        counts = {"live": 0, "cache_hit": 0, "fixture": 0}
        for e in evs:
            counts[e["source"]] += 1
        total = len(evs)
        return {**counts, "total": total, "live_share": (counts["live"] / total) if total else None,
                "by_call": {e["llm_call_id"]: e["source"] for e in evs}}

    def source_of(self, llm_call_id: str) -> str | None:
        """Source of the most recent event for ``llm_call_id`` (live | cache_hit | fixture), or None."""
        for e in reversed(self.events):
            if e["llm_call_id"] == llm_call_id:
                return e["source"]
        return None

    # -------------------------------------------------------------- public API

    def call(
        self,
        prompt_name: str,
        prompt_version: int,
        messages: list[dict[str, str]],
        response_model: Type[T],
        temperature: float | None = None,
        seed: int | None = None,
        extra_inputs: dict | None = None,
        context: dict | None = None,
        batch_info: dict | None = None,
        barrier_scan: str | None = None,
    ) -> tuple[T, str]:
        """Execute an LLM call and return ``(parsed_response, llm_call_id)``.

        The call is subject to disk caching.  In mock mode the response is
        loaded from the fixture directory with no network access.

        ``context`` (D054) = ``{scenario_id, turn_idx, persona_seed}``; it is part of the cache key, so the
        cache is exact replay only and no response is shared between conversations or turns.
        """
        so_key = "prompt_json" if self._structured == "prompt_json" else None   # D062: not in the key otherwise
        scan = {"barrier_scan": barrier_scan} if barrier_scan else {}   # D070: hidden-token scan result, provenance only
        cache_key = self._cache_key(
            self._model, prompt_name, prompt_version, messages, extra_inputs, seed,
            backend=self._backend, provider=self._provider, context=context, structured_output=so_key,
        )
        base_extra = {"batch": batch_info, "structured_output": {"configured": self._structured,
                                                                 "effective": self._effective_mode()},
                      "est_input_tokens": self._input_estimate(messages, response_model), **scan}

        # 1. Cache hit (skipped entirely with fresh=True) --------------------
        cached = None if self._fresh else self._read_cache(cache_key)
        if cached is not None and self.is_live and cached.get("origin") == "fixture":
            cached = None   # D054: a fixture-derived entry is never a valid live answer
        if cached is not None:
            logger.debug("Cache hit for %s.v%d key=%s", prompt_name, prompt_version, cache_key[:12])
            parsed = response_model.model_validate(cached["response"])
            self._log_event(cache_key, prompt_name, prompt_version, "cache_hit", context, extra=base_extra)
            return parsed, cache_key

        # 2. Mock mode -------------------------------------------------------
        if self._mode == "mock":
            out = self._load_fixture(prompt_name, prompt_version, response_model, cache_key)
            self._log_event(cache_key, prompt_name, prompt_version, "fixture", context, extra=base_extra)
            return out

        # 3. Live / record mode ----------------------------------------------
        self._reset_call_state()
        self._prompt_name = prompt_name
        self._last_key = {}
        ec = (batch_info or {}).get("est_completion_tokens")
        self._expected_completion = int(ec) if ec else None
        try:
            raw_content = self._api_call(
                messages, temperature, seed, response_model
            )

            # Validate with retries
            parsed, raw_content, stripped = self._validate_loop(
                raw_content, messages, temperature, seed, response_model, prompt_name, prompt_version
            )
        except (LLMNetworkError, LLMValidationError, LLMBudgetError) as exc:
            so = dict(self._so_result or base_extra["structured_output"])
            so.update(honoured=False if isinstance(exc, LLMNetworkError) and getattr(exc, "status", None) == 400
                      else so.get("honoured"),
                      fallback=f"{type(exc).__name__}:{getattr(exc, 'status', None)}", error=str(exc)[:300])
            self._log_event(cache_key, prompt_name, prompt_version, "failed", context, self._http_attempts,
                            {**self._token_extra(base_extra), "batch": batch_info, "structured_output": so, **scan})
            raise

        llm_call_id = cache_key

        # Write to disk cache
        cache_entry = {
            "llm_call_id": llm_call_id,
            "prompt_name": prompt_name,
            "prompt_version": prompt_version,
            "model": self._model,
            "backend": self._backend,
            "provider": self._provider,
            "cache_key": cache_key,
            "response": json.loads(parsed.model_dump_json()),
            "raw_content": raw_content,
            "validated_text": stripped,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "origin": "live",
            "context": {k: v for k, v in context.items() if k != "run_id"} if context else context,
            "tokens": self._token_extra(base_extra),
            "structured_output": self._so_result,
            **scan,
        }
        self._write_cache(cache_key, cache_entry)
        self._log_event(cache_key, prompt_name, prompt_version, "live", context, self._http_attempts,
                        {**self._token_extra(base_extra), "batch": batch_info, "structured_output": self._so_result, **scan})

        # Record mode: also write fixture
        if self._mode == "record":
            self._write_fixture(prompt_name, llm_call_id, cache_entry)

        return parsed, llm_call_id

    # --------------------------------------------------------- cache key

    @staticmethod
    def _cache_key(
        model: str,
        prompt_name: str,
        prompt_version: int,
        messages: list[dict[str, str]],
        extra_inputs: dict | None,
        seed: int | None,
        backend: str = "",
        provider: dict | None = None,
        context: dict | None = None,
        structured_output: str | None = None,
    ) -> str:
        payload = {
            "model": model,
            "backend": backend,
            "provider": provider or {},
            "prompt_name": prompt_name,
            "prompt_version": prompt_version,
            "messages": messages,
            "extra_inputs": extra_inputs or {},
            "seed": seed,
        }
        if context:     # D054; absent when None so context-free keys (and their fixtures) are unchanged
            payload["context"] = {k: context.get(k) for k in ("scenario_id", "turn_idx", "persona_seed")}
        if structured_output:   # D062: only prompt_json adds a field, so every existing key is unchanged
            payload["structured_output"] = structured_output
        canonical = json.dumps(
            payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    # --------------------------------------------------------- disk cache I/O

    def _cache_path(self, key: str) -> Path:
        return self._cache_dir / key[:2] / f"{key}.json"

    def _read_cache(self, key: str) -> dict | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Corrupt cache entry %s: %s", path, exc)
            return None

    def _write_cache(self, key: str, entry: dict) -> None:
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # --------------------------------------------------------- fixture I/O

    def _fixture_path(self, prompt_name: str, llm_call_id: str) -> Path:
        return self._fixture_dir / prompt_name / f"{llm_call_id}.json"

    def _load_fixture(
        self,
        prompt_name: str,
        prompt_version: int,
        response_model: Type[T],
        cache_key: str,
    ) -> tuple[T, str]:
        path = self._fixture_path(prompt_name, cache_key)
        if not path.exists():
            raise FileNotFoundError(
                f"Mock fixture not found: expected {path} "
                f"(llm_call_id={cache_key}). "
                f"Run with GTGEN_LLM_MODE=record to capture a fixture."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        llm_call_id = cache_key
        try:
            parsed = response_model.model_validate(data["response"])
        except ValidationError as exc:
            raise LLMValidationError(
                prompt_name=prompt_name,
                prompt_version=prompt_version,
                errors=exc.errors(),
                raw_content=json.dumps(data.get("response", {})),
            ) from exc

        # Populate disk cache so subsequent identical calls hit the cache
        cache_entry = {
            "llm_call_id": llm_call_id,
            "prompt_name": prompt_name,
            "prompt_version": prompt_version,
            "model": self._model,
            "backend": self._backend,
            "provider": self._provider,
            "cache_key": cache_key,
            "response": data["response"],
            "created_at": data.get("created_at", datetime.now(timezone.utc).isoformat()),
            "origin": "fixture",
        }
        self._write_cache(cache_key, cache_entry)

        return parsed, llm_call_id

    def _write_fixture(
        self, prompt_name: str, llm_call_id: str, entry: dict
    ) -> None:
        path = self._fixture_path(prompt_name, llm_call_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # --------------------------------------------------------- OpenRouter API

    def _resolve_api_key(self) -> tuple[str, str, str | None]:
        """Return ``(env_var_name, key_value, role)`` for the current prompt (D063/D084).

        If the role has a key pool, round-robin over its usable variables (each keeps its own limiter); otherwise
        use the role's variable if set, non-empty and not a placeholder, else the default variable.  If nothing is
        usable raise naming the VARIABLE (never a value)."""
        role = role_for_prompt(self._prompt_name) if self._prompt_name else None
        if role:
            pool = self._api_key_pools.get(role)
            if pool:
                usable = [name for name in pool if _usable_key(os.environ.get(name))]
                if usable:
                    with self._key_lock:
                        cursor = self._key_cursor.get(role, 0)
                        name = usable[cursor % len(usable)]
                        self._key_cursor[role] = cursor + 1
                    return name, os.environ[name], role
        candidates: list[str] = []
        role_env = self._api_key_roles.get(role) if role else None
        if role_env:
            candidates.append(role_env)
        candidates.append(self._api_key_env)
        for name in candidates:
            val = os.environ.get(name)
            if _usable_key(val):
                return name, val, role      # type: ignore[return-value]
        tried = " / ".join(candidates)
        raise EnvironmentError(f"Environment variable {tried} is not set.")

    def _get_api_key(self) -> str:
        return self._resolve_api_key()[1]

    def _all_key_values(self) -> list[str]:
        names = [self._api_key_env, *self._api_key_roles.values()]
        for pool in self._api_key_pools.values():
            names.extend(pool)
        return [v for v in (os.environ.get(n) for n in names) if _usable_key(v)]

    def _redact(self, text: str, api_key: str = "") -> str:
        """Ensure no configured API key (default, any role, or any pool) can appear in exception/log text."""
        for k in sorted({*self._all_key_values(), *([api_key] if api_key else [])}, key=len, reverse=True):
            text = text.replace(k, "[REDACTED]")
        return text

    # ------------------------------------------------------------ structured output / token budget (D062)

    def _effective_mode(self) -> str:
        """``auto`` behaves as ``json_schema`` (response_format sent); ``prompt_json`` sends no response_format."""
        return "prompt_json" if self._structured == "prompt_json" else "json_schema"

    def _prompt_json_instruction(self, response_model: Type[BaseModel]) -> str:
        return ("Return ONLY one JSON object that conforms to the following JSON Schema. No prose, no markdown, no "
                "code fences.\nJSON Schema: " + schema_text(response_model))

    def _wire_messages(self, messages: list[dict[str, str]], response_model: Type[BaseModel]) -> list[dict[str, str]]:
        """Messages as sent.  prompt_json: the schema is appended to the FIRST message (never part of the cache key)."""
        if self._effective_mode() != "prompt_json" or not messages:
            return messages
        out = [dict(m) for m in messages]
        out[0]["content"] = out[0].get("content", "") + "\n\n" + self._prompt_json_instruction(response_model)
        return out

    def _input_estimate(self, messages: list[dict[str, str]], response_model: Type[BaseModel]) -> int:
        """Estimated input tokens of the request as it would be sent: every message (incl. the prompt_json schema
        instruction) plus, in json_schema mode, the schema carried in ``response_format``."""
        wire = self._wire_messages(messages, response_model)
        extra = schema_text(response_model) if self._effective_mode() == "json_schema" else ""
        return estimate_messages_tokens(wire, extra)

    def _build_budget(
        self, messages: list[dict[str, str]], response_model: Type[BaseModel]
    ) -> int:
        """Return max_tokens for this request or raise LLMBudgetError.

        ``max_tokens = min(max_completion_tokens, max_tokens_per_call - est_input)``; the sum est_input +
        max_tokens therefore never exceeds ``max_tokens_per_call`` (<= token_limit_total, the endpoint's
        input+output limit).  Fails (nothing is sent) when the room left is below ``min_completion_tokens``.
        """
        est_prompt = self._input_estimate(messages, response_model)
        max_tokens = min(self._max_tokens, self._max_tokens_per_call - est_prompt)
        if max_tokens < self._min_completion:
            raise LLMBudgetError(
                f"Prompt too large: ~{est_prompt} est. prompt tokens leaves {max_tokens} "
                f"completion tokens (< min_completion_tokens={self._min_completion}, "
                f"max_tokens_per_call={self._max_tokens_per_call}). "
                "The batch is too large; split it into smaller batches."
            )
        return max_tokens

    def _expected_completion_tokens(self, max_tokens: int) -> int:
        """Completion tokens reserved in the pacer: the caller's planned completion (batch_info
        est_completion_tokens) if known, else min(max_tokens, batch_completion_target); never above max_tokens."""
        exp = self._expected_completion if self._expected_completion else self._batch_completion_target
        return max(0, min(exp, max_tokens))

    def _token_extra(self, base: dict) -> dict:
        """Provenance fields: estimate vs. the endpoint-reported usage, summed over every HTTP request of the call."""
        reqs = self._requests
        est_in = sum(r["est_input_tokens"] for r in reqs)
        usage = {k: sum((r.get("usage") or {}).get(k, 0) for r in reqs)
                 for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
        have = any(r.get("usage") for r in reqs)
        return {
            "est_input_tokens": reqs[0]["est_input_tokens"] if reqs else base["est_input_tokens"],
            "max_tokens_sent": reqs[0]["max_tokens"] if reqs else None,
            "est_input_tokens_all_requests": est_in,
            "usage": usage if have else None,
            "estimator_ratio": round(est_in / usage["prompt_tokens"], 3) if have and usage["prompt_tokens"] else None,
            "requests": [dict(r) for r in reqs],
        }

    def _classify_first(self, raw: str, response_model: Type[BaseModel]) -> dict:
        """Was the structured-output path honoured?  Judged on the FIRST reply of the call."""
        mode = self._effective_mode()
        base = {"configured": self._structured, "effective": mode, "response_format_sent": mode == "json_schema"}
        bare = raw.strip()
        leading = first_json_object(raw) if mode != "prompt_json" else None
        if mode == "prompt_json":
            candidate = extract_json(raw)
        else:
            candidate = leading[0] if leading is not None else strip_code_fence(raw)
        try:
            response_model.model_validate_json(tolerant_json_text(candidate))
            valid = True
        except ValidationError:
            valid = False
        if mode == "prompt_json":
            return {**base, "honoured": None, "fallback": None, "first_attempt_valid": valid}
        if valid and leading is not None and not leading[1] and not bare.startswith("```"):
            return {**base, "honoured": True, "fallback": None, "first_attempt_valid": True}
        if bare.startswith("```"):
            why = "code_fence"
        elif leading is not None:
            why = "trailing_text" if valid else "schema_invalid"
        else:
            try:
                json.loads(bare)
                why = "schema_invalid"
            except ValueError:
                why = "not_json"
        return {**base, "honoured": False, "fallback": why, "first_attempt_valid": valid}

    def _retry_after_s(self, resp: Any) -> float | None:
        """Parse Retry-After (delta-seconds or HTTP-date); None if absent/invalid."""
        headers = getattr(resp, "headers", None) or {}
        value = None
        for k, v in headers.items():
            if k.lower() == "retry-after":
                value = str(v).strip()
                break
        if not value:
            return None
        try:
            return max(float(value), 0.0)
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)
        except (TypeError, ValueError):
            return None

    def _api_call(
        self,
        messages: list[dict[str, str]],
        temperature: float | None,
        seed: int | None,
        response_model: Type[BaseModel],
    ) -> str:
        """Send a chat completion request to OpenRouter. Returns raw content string."""
        key_env, api_key, key_role = self._resolve_api_key()
        self._last_key = {"api_key_role": key_role, "api_key_env": key_env}
        limiter = _LIMITER.get(key_env)
        url = f"{self._base_url}/chat/completions"
        max_tokens = self._build_budget(messages, response_model)  # before any HTTP; raises LLMBudgetError

        body: dict[str, Any] = {
            "model": self._model,
            "messages": self._wire_messages(messages, response_model),
            "max_tokens": max_tokens,
        }
        if self._effective_mode() == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": response_model.model_json_schema(),
                },
            }
        if temperature is not None:
            body["temperature"] = temperature
        if seed is not None:
            body["seed"] = seed
        if self._provider:
            body["provider"] = self._provider
        if self._disable_reasoning:
            body["reasoning"] = {"enabled": False}
        body.update(self._extra_body)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if self._gemini:      # replaces the OpenAI-style request built above
            url = f"{self._base_url}/models/{self._model}:generateContent"
            body = _gemini_body(self._wire_messages(messages, response_model), temperature, max_tokens,
                                self._disable_reasoning)
            headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

        record = {"est_input_tokens": self._input_estimate(messages, response_model), "max_tokens": max_tokens,
                  "usage": None}
        self._requests.append(record)
        resp = None
        n_total = self._http_max_attempts
        for attempt in range(1 + n_total):
            if self._tpm_budget is not None:      # D068: every attempt (incl. retries) re-reserves
                # D105: Google's TPM counts input only (https://ai.google.dev/gemini-api/docs/rate-limits)
                completion = 0 if self._gemini else self._expected_completion_tokens(max_tokens)
                limiter.reserve_tokens(record["est_input_tokens"] + completion,
                                       self._tpm_budget, key_role or key_env)
            limiter.acquire(self._min_interval_s)
            self._http_attempts += 1
            linear = self._backoff_s[min(attempt, len(self._backoff_s) - 1)]
            try:
                resp = httpx.post(
                    url,
                    json=body,
                    headers=headers,
                    timeout=self._timeout_s,
                )
            except httpx.RequestError as exc:  # timeouts, ConnectError, RemoteProtocolError, ReadError ...
                reason = type(exc).__name__
                err = f"OpenRouter request failed ({reason}): {self._redact(str(exc), api_key)}"
                wait = compute_wait_s(linear, None, self._margin_s, self._max_wait_s)
                logger.warning("OpenRouter %s; attempt %d/%d; body=%r; waiting %.1fs",
                               reason, attempt + 1, 1 + n_total, self._redact(str(exc), api_key)[:600], wait)
                if attempt >= n_total:
                    raise LLMNetworkError(err) from None
                limiter.sleep(wait)
                continue
            status = resp.status_code
            if status < 400:
                break
            text = self._redact(getattr(resp, "text", "") or "", api_key)
            if status in self._non_retryable or not (status == 429 or 500 <= status < 600):
                logger.warning("OpenRouter HTTP %s (not retryable); body=%r", status, text[:600])
                err4 = LLMNetworkError(f"OpenRouter API returned {status}: {text}")
                err4.status = status  # type: ignore[attr-defined]
                raise err4 from None
            hint: float | None = None
            if self._respect_retry_after:
                cands = [h for h in (self._retry_after_s(resp), body_retry_hint(text)) if h is not None]
                hint = max(cands) if cands else None
                if hint is not None and hint > self._max_retry_after_s:
                    logger.warning("OpenRouter HTTP %s; retry hint %.0fs > max_retry_after_s=%.0f; failing fast; body=%r",
                                   status, hint, self._max_retry_after_s, text[:600])
                    errf = LLMNetworkError(
                        f"OpenRouter API returned {status} with retry hint {hint:.0f}s "
                        f"(> max_retry_after_s={self._max_retry_after_s:.0f}); likely the "
                        f"daily free quota or a hard rate limit. Failing fast: {text}"
                    )
                    errf.status = status  # type: ignore[attr-defined]
                    raise errf from None
            wait = compute_wait_s(linear, hint, self._margin_s, self._max_wait_s)
            logger.warning("OpenRouter HTTP %s; attempt %d/%d; body=%r; waiting %.1fs",
                           status, attempt + 1, 1 + n_total, text[:600], wait)
            if attempt >= n_total:
                errx = LLMNetworkError(f"OpenRouter API returned {status} after {n_total} retries: {text}")
                errx.status = status  # type: ignore[attr-defined]
                raise errx from None
            limiter.sleep(wait)

        assert resp is not None
        data = resp.json()
        if self._gemini:
            data = _from_gemini(data)

        # Log provider information if available
        provider = data.get("provider") or resp.headers.get("x-provider")
        if provider:
            logger.info("OpenRouter provider: %s", provider)

        choices = data.get("choices") or []
        usage = data.get("usage")
        if isinstance(usage, dict):    # actual tokens as counted by the endpoint, next to the estimate
            pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            record["usage"] = {"prompt_tokens": pt, "completion_tokens": ct,
                               "total_tokens": int(usage.get("total_tokens") or pt + ct)}
        msg = (choices[0].get("message") or {}) if choices and isinstance(choices[0], dict) else {}
        content = msg.get("content") or ""
        if not content:
            # D067: diagnostic, then a retryable validation failure (never a crash)
            reasoning = msg.get("reasoning") or ""
            diag = (f"empty response content: choices={len(choices)}, "
                    f"finish_reason={choices[0].get('finish_reason') if choices and isinstance(choices[0], dict) else None}, "
                    f"model={data.get('model')}, provider={data.get('provider')}, usage={usage}, "
                    f"reasoning_len={len(reasoning) if isinstance(reasoning, str) else 0}, "
                    f"error={self._redact(json.dumps(data.get('error'), default=str), api_key)[:300] if data.get('error') else None}")
            logger.warning("OpenRouter %s", diag)
            self._empty_diag = diag
            return ""
        self._empty_diag = None

        return content

    # --------------------------------------------------------- validation + retry

    def _validate_with_retries(
        self,
        raw_content: str,
        messages: list[dict[str, str]],
        temperature: float | None,
        seed: int | None,
        response_model: Type[T],
        prompt_name: str,
        prompt_version: int,
    ) -> tuple[T, list]:
        """Validate ``raw_content``; on failure, retry with error feedback."""
        parsed, _raw, _stripped = self._validate_loop(
            raw_content, messages, temperature, seed, response_model, prompt_name, prompt_version
        )
        return parsed, []

    def _validate_loop(
        self,
        raw_content: str,
        messages: list[dict[str, str]],
        temperature: float | None,
        seed: int | None,
        response_model: Type[T],
        prompt_name: str,
        prompt_version: int,
    ) -> tuple[T, str, str]:
        """Return ``(parsed, raw_content, validated_text)`` of the accepted attempt.

        One surrounding code fence is stripped before validation.  The retry
        prompt carries the unmodified raw reply; the token budget is recomputed
        in ``_api_call`` on every attempt.
        """
        last_errors: list = []
        base_messages = list(messages)

        self._so_result = self._classify_first(raw_content, response_model)
        for attempt in range(1 + self._max_retries):
            if self._effective_mode() == "prompt_json":
                candidate = extract_json(raw_content)
            else:
                leading = first_json_object(raw_content)
                candidate = leading[0] if leading is not None else strip_code_fence(raw_content)
            stripped = tolerant_json_text(candidate)
            try:
                parsed = response_model.model_validate_json(stripped)
                self._so_result["validation_retries"] = attempt
                return parsed, raw_content, stripped
            except ValidationError as exc:
                last_errors = exc.errors()
                if not raw_content.strip():
                    last_errors = [{"type": "empty_content", "msg": getattr(self, "_empty_diag", None) or "empty response content"}]
                logger.warning(
                    "Validation attempt %d/%d failed for %s.v%d: %s",
                    attempt + 1,
                    1 + self._max_retries,
                    prompt_name,
                    prompt_version,
                    last_errors,
                )
                if attempt < self._max_retries:
                    correction = {
                        "role": "user",
                        "content": (
                            "Your previous response failed JSON schema validation. "
                            f"Errors: {json.dumps(last_errors, default=str)}. "
                            "Please fix and return valid JSON."
                        ),
                    }
                    # Keep only the latest rejected reply (not the whole history) and drop it if the pair would
                    # leave too little completion room; unbounded accumulation is what hit LLMBudgetError.
                    retry = base_messages + ([{"role": "assistant", "content": raw_content}]
                                             if raw_content.strip() else []) + [correction]
                    try:
                        self._build_budget(retry, response_model)
                        retry_messages = retry
                    except LLMBudgetError:
                        retry_messages = base_messages + [correction]
                    raw_content = self._api_call(
                        retry_messages, temperature, seed, response_model
                    )

        self._so_result["validation_retries"] = self._max_retries
        raise LLMValidationError(
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            errors=last_errors,
            raw_content=raw_content,
        )
