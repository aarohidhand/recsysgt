# Prompting playbook: driving gtgen with Claude Code

You talk to **one** Claude Code session (the coordinator). It delegates to the five subagents in
`.claude/agents/`. Subagents start with no memory of your conversation, so every delegation needs
the full context. You can let the coordinator delegate on its own, or name an agent explicitly
("use the data-steward subagent to …"). `/agents` lists them, and you can edit their tools or
model there.

## 0. Setup (once)
```bash
cd negotiation-gt
cp /path/to/*.csv data/raw/                 # the six canonical CSVs
export OPENROUTER_API_KEY=sk-or-...          # OpenRouter key; add your Google AI Studio key as BYOK
                                             # in OpenRouter → Settings → Integrations
claude                                       # start Claude Code in the repo root
```
Answer the open question in CLAUDE.md §1.1 first: does `golden_param=True` mean HARD?

## 1. Capabilities at a glance

| Agent | Model | Tools | Can edit | Use it for |
|---|---|---|---|---|
| main session (you talk to it) | your choice | all | contracts, config, cli, CLAUDE.md, agents | planning, delegation, integration, running the pipeline, settling conflicts |
| data-steward | sonnet | Read/Write/Edit/Bash/Grep/Glob | `src/gtgen/data`, `tests/data` | CSV loading, canonical columns, joins, CatalogueView, profiling |
| llm-engineer | sonnet | + WebFetch | `src/gtgen/llm`, `prompts`, LLM tests/fixtures | OpenRouter client, cache, mock mode, all 11 runtime prompts |
| matching-engineer | sonnet | Read/Write/Edit/Bash/Grep/Glob | `matching`, `ranking`, `eval` | compliance, relaxation search, ranking matrix, probabilities, metrics |
| dialogue-engineer | opus | Read/Write/Edit/Bash/Grep/Glob | `src/gtgen/dialogue`, `tests/dialogue` | Buyer simulator, Negotiator, turn loop, barrier, GT assembly |
| gt-auditor | opus | Read/Grep/Glob/Bash/Write | `reports/` only | independent audit of code and generated GT |

## 2. Prompt template for every delegation
```
Use the <agent> subagent.
Task: <one concrete outcome>
Contracts: <class names from src/gtgen/contracts.py it must produce/consume>
Files: <paths to create/edit; paths to read>
Inputs: <data paths, settings keys, upstream functions now available>
Acceptance: <tests that must pass; command to run>
Constraints: <relevant CLAUDE.md rule numbers, e.g. §1.5 barrier, §1.7 PENDING>
Return: files changed, commands run with real output, open questions.
```

## 3. The build, in order (paste these to the main session)

**Step 1 — contracts check (main session)**
> Read CLAUDE.md, docs/canonical_data_analysis_guide.md and src/gtgen/contracts.py. List any field
> the algorithm in CLAUDE.md §3 needs that the contracts lack. Propose changes; don't delegate yet.

**Step 2 — in parallel**
> Delegate in parallel: (a) data-steward: implement loaders, joins, `build_catalogue_view`, and
> `profile` per its agent file, with fixture CSVs and tests; then run profile on data/raw and save it
> to docs/data_profile.md. (b) llm-engineer: implement client, cache and mock/record modes only (no
> prompts yet), with tests. Their paths don't overlap.

**Step 3 — in parallel**
> Delegate in parallel: (a) matching-engineer: compliance, relaxation search, ranking and eval,
> per its agent file, hand-computed tests. (b) llm-engineer: write all prompts listed in its agent
> file at v1, plus mock fixtures for the pilot bid <BID_NUMBER>.

**Step 4 — dialogue**
> Use the dialogue-engineer subagent to implement buyer.py, negotiator.py, loop.py and
> assemble.py, importing gtgen.matching, gtgen.ranking and gtgen.llm (now done; list their public
> functions in the prompt). Include all three barrier tests. Then add `pilot` to cli.py yourself.

**Step 5 — pilot + audit**
> Run `pytest -q`, then `python -m gtgen.cli pilot --mock`, then a live pilot: 1 bid × 3 personas.
> Then use the gt-auditor subagent on out/gt/<run_id>. Summarise the verdict and send each issue to
> its owning agent.

**Step 6 — scale**
> Run all 6 bids × `personas_per_bid` live. Audit again. Run eval and save metrics to reports/.

## 4. Useful one-off prompts
- *Buyer personality spread:* "Ask gt-auditor to check the τ histogram for run X. If it fails
  `tau_spread_check`, ask llm-engineer to revise the persona prompt to v2 (keep v1 cached)."
- *Relaxation reasoning:* "For scenario S turn 3, show me every RelaxationOption the negotiator
  considered, with candidates_gained and cost, and why the top one was chosen."
- *Government leak:* "gt-auditor found 'tender' in 4 buyer utterances. Send the examples to
  llm-engineer and have it tighten buyer_reply and add a post-generation blocklist retry."
- *Contract change:* "Add `delivery_days` to CandidateView. Update contracts.py yourself, then tell
  data-steward and dialogue-engineer exactly what changed."

## 5. Rules of thumb
- Name an agent when you care who does it; describe the outcome when you don't.
- Never ask one agent to edit another's files — ask the coordinator to route the change.
- Ask for "the command you ran and its real output", not "confirm it works".
- Keep the main session's context small: ask subagents for summaries, not full file dumps.
- Use `--mock` until the logic is right, then go live; LLM cache makes reruns cheap and identical.
