#!/usr/bin/env python3
"""PreToolUse hook: block Edit/Write outside an agent's owned paths (CLAUDE.md §5).

Usage in agent frontmatter:  python3 "$CLAUDE_PROJECT_DIR/scripts/check_owner.py" <agent-name>
Exit 0 = allow, exit 2 = block (stderr is shown to the agent).
"""
import json
import os
import re
import sys

# A subagent run with isolation="worktree" writes into .claude/worktrees/<agent-id>/<path>.
# Depending on how CLAUDE_PROJECT_DIR resolves for that invocation, `rel` below may come out
# either as the bare in-repo path (CLAUDE_PROJECT_DIR = the worktree root — the common case) or
# with the worktree container prefix still attached (CLAUDE_PROJECT_DIR = the main checkout root).
# Strip the prefix so ownership is judged on the same logical path either way.
_WORKTREE_PREFIX = re.compile(r"^\.claude/worktrees/[^/]+/(.*)$")

OWNERS = {
    "orchestrator": ["src/gtgen/contracts.py", "src/gtgen/cli.py", "src/gtgen/__main__.py",
                     "config/settings.yaml", "CLAUDE.md", ".claude/", "scripts/", "docs/",
                     ".gitignore", ".env", "requirements.txt", "pyproject.toml"],
    "data-steward": ["src/gtgen/data/", "tests/data/", "docs/data_profile.md", "docs/coverage.md", "docs/coverage_llm.md"],
    "llm-engineer": ["src/gtgen/llm/", "prompts/", "tests/llm/", "tests/fixtures/llm/"],
    "matching-engineer": ["src/gtgen/matching/", "src/gtgen/ranking/", "src/gtgen/eval/",
                          "tests/matching/", "tests/ranking/", "tests/eval/"],
    "dialogue-engineer": ["src/gtgen/dialogue/", "tests/dialogue/"],
    "gt-auditor": ["reports/"],
}
EXCEPT = {"orchestrator": ["docs/data_profile.md", "docs/coverage.md", "docs/coverage_llm.md"]}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    # Claude Code sends agent_type in the hook input; inside a subagent it is the subagent's own
    # type, and it takes precedence over argv. argv comes from the FIRING agent's own frontmatter
    # hook command, which stays bound to the main thread's (--agent orchestrator) hook while a
    # subagent runs — so argv alone reads "orchestrator" for every subagent write, not the
    # subagent's real type. Prefer agent_type; fall back to argv, then "orchestrator".
    argv_agent = sys.argv[1] if len(sys.argv) > 1 else ""
    agent = payload.get("agent_type") or argv_agent or "orchestrator"

    proj = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    if os.path.exists(os.path.join(proj, ".hook_debug_on")):   # touch this file to log decisions
        with open(os.path.join(proj, ".hook_debug.log"), "a") as f:
            f.write(json.dumps({
                "argv": sys.argv[1:], "agent_type": payload.get("agent_type"),
                "agent_id": payload.get("agent_id"),
                "path": (payload.get("tool_input") or {}).get("file_path"),
                "resolved": agent,
            }) + "\n")

    ti = payload.get("tool_input", {}) or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
    if not path:
        return 0
    root = os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    rel = os.path.relpath(os.path.realpath(os.path.join(root, path)), root).replace(os.sep, "/")
    if rel.startswith("../"):
        print(f"[ownership] {agent} may not write outside the repo: {rel}", file=sys.stderr)
        return 2
    m = _WORKTREE_PREFIX.match(rel)
    if m:
        rel = m.group(1)
    allowed = OWNERS.get(agent, [])
    if any(rel == p.rstrip("/") or rel.startswith(p) for p in EXCEPT.get(agent, [])):
        allowed = []
    if any(rel == p.rstrip("/") or rel.startswith(p) for p in allowed):
        return 0
    print(f"[ownership] {agent} does not own '{rel}'. Stop and report the needed change to "
          f"the orchestrator (CLAUDE.md §5).", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
