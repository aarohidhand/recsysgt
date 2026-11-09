"""Connectivity check: ONE tiny chat request per API-key role (6 requests max, no retries). Never prints keys.
Usage: python scripts/check_endpoints.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
cfg = yaml.safe_load((ROOT / "config/settings.yaml").read_text())["llm"]
backend = cfg["backends"][os.environ.get("GTGEN_LLM_BACKEND") or cfg.get("backend", next(iter(cfg["backends"])))] if "backends" in cfg else {}
provider = backend.get("provider") or {"order": ["google-ai-studio"], "allow_fallbacks": False}
if backend.get("api") == "gemini":       # direct Google backend: one key, native generateContent
    env = backend["api_key_env"]
    key = os.environ.get(env, "")
    if not key or "REPLACE_ME" in key:
        print(f"google      {env}: NOT SET")
        sys.exit(1)
    r = httpx.post(f"{backend['base_url']}/models/{backend['model']}:generateContent", timeout=120,
                   headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                   json={"contents": [{"role": "user", "parts": [{"text": "Reply with the single word: ok"}]}],
                         "generationConfig": {"maxOutputTokens": 20, "thinkingConfig": {"thinkingLevel": "minimal"}}})
    print(f"google      {env}: HTTP {r.status_code} {'ok' if r.status_code == 200 else r.text[:200]}")
    sys.exit(0 if r.status_code == 200 else 1)
roles = {"default": cfg["api_key_env"], **cfg["api_key_roles"]}
bad = 0
for role, env in roles.items():
    key = os.environ.get(env, "")
    if not key or "REPLACE_ME" in key:
        print(f"{role:11s} {env}: NOT SET"); bad += 1; continue
    r = httpx.post(f"{cfg['base_url']}/chat/completions", timeout=120,
                   headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                   json={"model": cfg["model"], "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                         "max_tokens": 20, "provider": provider})
    if r.status_code == 200:
        j = r.json()
        print(f"{role:11s} {env}: HTTP 200 provider={j.get('provider')} model={j.get('model')} "
              f"usage={j.get('usage', {}).get('total_tokens')} reply={(j['choices'][0]['message'].get('content') or '')[:30]!r}")
    else:
        bad += 1; print(f"{role:11s} {env}: HTTP {r.status_code} {r.text[:200]}")
sys.exit(1 if bad else 0)
