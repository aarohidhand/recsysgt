"""Prompt file loading, header validation and rendering (prompts/<name>.v<N>.md)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROMPT_NAMES = (
    "reframe", "doc_classify", "persona", "buyer_opening", "buyer_reply",
    "negotiator_extract", "negotiator_reply", "align", "normalise",
    "reveal_check", "claim_check",
    "negotiator_clarify", "buyer_reveal", "negotiator_interpret",
    "doc_claim", "negotiator_final",
)
# Prompts whose temperature key may be absent from settings; the named prompt's temperature is used instead (D069).
TEMPERATURE_FALLBACK = {"negotiator_final": "negotiator_reply"}
PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
_REQUIRED_HEADER = ("name", "version", "purpose", "input_vars", "output_schema", "temperature_key")
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


@dataclass(frozen=True)
class PromptSpec:
    name: str
    version: int
    purpose: str
    input_vars: tuple[str, ...]
    output_schema: str
    temperature_key: str
    body: str
    per_conversation_inputs: tuple[str, ...] = ()    # D054: inputs that differ per conversation / turn

    def render(self, **variables: Any) -> str:
        """Fill ``{{var}}`` placeholders. Non-string values are rendered as compact JSON."""
        missing = set(self.input_vars) - set(variables)
        extra = set(variables) - set(self.input_vars)
        if missing or extra:
            raise ValueError(
                f"prompt {self.name}: missing vars {sorted(missing)}, unexpected vars {sorted(extra)}"
            )
        values = {
            k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            for k, v in variables.items()
        }
        return _PLACEHOLDER.sub(lambda m: values[m.group(1)], self.body).strip()


def parse_prompt(text: str, source: str = "<prompt>") -> PromptSpec:
    m = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.S)
    if not m:
        raise ValueError(f"{source}: missing '---' header block")
    header: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            raise ValueError(f"{source}: bad header line {line!r}")
        k, v = line.split(":", 1)
        header[k.strip()] = v.strip()
    for key in _REQUIRED_HEADER:
        if not header.get(key):
            raise ValueError(f"{source}: header key {key!r} missing")
    iv = header["input_vars"]
    if not (iv.startswith("[") and iv.endswith("]")):
        raise ValueError(f"{source}: input_vars must be a [list]")
    input_vars = tuple(x.strip() for x in iv[1:-1].split(",") if x.strip())
    pci = header.get("per_conversation_inputs", "")
    if pci and not (pci.startswith("[") and pci.endswith("]")):
        raise ValueError(f"{source}: per_conversation_inputs must be a [list]")
    per_conv = tuple(x.strip() for x in pci[1:-1].split(",") if x.strip()) if pci else ()
    if set(per_conv) - set(input_vars):
        raise ValueError(f"{source}: per_conversation_inputs {sorted(set(per_conv) - set(input_vars))} are not input_vars")
    body = m.group(2)
    used = set(_PLACEHOLDER.findall(body))
    if used != set(input_vars):
        raise ValueError(
            f"{source}: input_vars {sorted(input_vars)} do not match placeholders {sorted(used)}"
        )
    return PromptSpec(
        name=header["name"], version=int(header["version"]), purpose=header["purpose"],
        input_vars=input_vars, output_schema=header["output_schema"],
        temperature_key=header["temperature_key"], body=body,
        per_conversation_inputs=per_conv,
    )


def latest_version(name: str, prompts_dir: Path | None = None) -> int:
    """Highest N with a file prompts/<name>.vN.md."""
    found = [int(m.group(1)) for f in (prompts_dir or PROMPTS_DIR).glob(f"{name}.v*.md")
             if (m := re.fullmatch(re.escape(name) + r"\.v(\d+)\.md", f.name))]
    if not found:
        raise FileNotFoundError(f"no prompt file for {name!r}")
    return max(found)


def load_prompt(name: str, version: int | None = None, prompts_dir: Path | None = None) -> PromptSpec:
    """Load ``name`` at ``version``; ``None`` = the latest version on disk."""
    if version is None:
        version = latest_version(name, prompts_dir)
    path = (prompts_dir or PROMPTS_DIR) / f"{name}.v{version}.md"
    spec = parse_prompt(path.read_text(encoding="utf-8"), str(path))
    if spec.name != name or spec.version != version:
        raise ValueError(f"{path}: header name/version do not match file name")
    return spec
