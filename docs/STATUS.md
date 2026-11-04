# Project Status

**Milestone:** Step 6 re-plan (user prompt 2026-10-29): evidence-grounded briefs (D046 narrowed, D048), synthetic commercial terms
(D049), always-recommend with BEST_AVAILABLE (D050), doc-proof claims (D051), truth ranking on the final agreed state (D052).
Live pilot still ON HOLD. No gt-auditor audit of a run yet.
**Last verified:** 2026-10-31, `pytest -q` 849 passed. Model: google/gemma-4-26b-a4b-it:free via google-ai-studio ONLY (D062 reverts D058; nothing paid; limits 16,000 tokens/call total, 20 rpm). `pilot --mock` still yields 0 conversations (no D054 mock fixtures).
**Model for live runs:** `google/gemma-4-26b-a4b-it:free` (the `:free` variant) via OpenRouter, provider Google AI Studio (D022).

**UNBLOCKED 2026-10-29:** the D053 spec is at `docs/RELAXATION_LOGIC.md`. Contracts/settings for D053/D055/D056 landed (additive); D053 + D054 delegation in progress, then CLI wiring, then stage 1 live.

## Re-plan progress
| Step | Owner | State |
|---|---|---|
| 1 contracts, settings, decisions, CLAUDE.md (incl. D050 addendum) | coordinator | DONE |
| 2a coverage, roles, bands, terms, tightening (`data/scenario.py`) | data-steward | DONE |
| 2b persona v2, doc_claim prompt, quote verification | llm-engineer | DONE (no live run yet) |
| 3 matching: unscored cells, claims, synthetic evidence, match_original, BEST_AVAILABLE builder, D046 merge | matching-engineer | DONE |
| 4a dialogue: roles, claims, synthetic terms, evolving truth state, reference_ranking_initial | dialogue-engineer | DONE |
| 4b dialogue: always recommend (BEST_AVAILABLE), WRAP_UP turn, default-settings golden run | dialogue-engineer | DONE |
| eval cleanup: removed outcomes deleted from the contract, policy_compliance_rate bug fixed | matching-engineer + coordinator | DONE |
| 5a scenario assembly pipeline + live-call planner (`data/scenario_build.py`, `scenario_table.py`) | data-steward | DONE, `run` never executed |
| 5 B-style table for the 6 bids | data-steward | WAITS: D053 + D054 + CLI wiring first, then stage 1 (--fresh, bids 231682 + 488257, ~24 calls) |
| 3b calibration against tier targets, per-tier targets vs simulated outcomes | matching-engineer | Meaning settled (reading A, D050); its simulator must use the D053 front + buyer, so BLOCKED on D053 |
| Q3 low-coverage SOFT -> verification_only; verification_only_fail_blocks=true; BEST_AVAILABLE ranking by score + offered ids; COUNTER gain at the counter value | data-steward, dialogue-engineer, coordinator | DONE |
| D053 relaxation spec | matching-engineer, dialogue-engineer | IN PROGRESS: delegated |
| D054 live, conversation-specific prompting | llm-engineer, dialogue-engineer, coordinator | decided, waits for D053 |
| cli.py `pilot` wired to `build_scenario` + doc_claims, --live/--mock/--fresh | coordinator | WIRED (unverified live); mock yields 0 conversations; scenario_table has no --fresh planner mode yet |
| D057 cleanup of 6eb11b5; engine reads spec settings keys; D053 dialogue/golden tests | coordinator + specialists | DONE, 831 passed |
| gt-auditor full checklist on a live run | gt-auditor | NOT DONE (needs the live pilot) |

## Open before any live run (2026-10-31)
- User approval for stage 1. Plan (--fresh, 1 seed/bid, 26B): 488257 + 341930 = 55 logical calls, 165 HTTP worst case; 231682 + 488257 = 32 / 99.
- 231682: runners.reframe discards a correct DROP when the echoed value is merely reworded (government MSE term stays active, rule 4 risk). Fix is llm-engineer's (runners.py); not yet done.
- D062: token budget 15,000/call, dynamic batching, structured-output check and a 5-request smoke call pending.
- Auditor golden-test review (reports/golden_test_diff_review.md): W1-W5 fixed; add a client-boundary seed assertion in tests/llm (llm-engineer); real persona_seed reaches Negotiator ctx (cache key/log only).
- check_approval compares to logical calls, not HTTP requests.

## Stage 1 status (2026-10-31, late)
- Done: reframe DROP fix (D-bug), D060 strata + company uniqueness wired into build_scenario and cli pilot, D061/D060 eval modules, negotiator cache seed derived, check_approval on HTTP worst case, cmd_eval fixed, pilot `--max-requests` (required for --live; first failing call stops the run with the exact error).
- BLOCKER for the live run: the approved cap 165 does not cover the plan. Prep alone (488257 + 341930, --fresh) is now 61 upper calls / 183 worst-case HTTP (regeneration bound added), and the plan EXCLUDES the dialogue (golden run = 28 LLM calls per 15-turn conversation with 5 constraints; real bids will need more). Needs a new cap from the user.

## Waiting for the user
1. Live table: approve the two-stage run (24-call smoke test on bids 231682 + 488257, then 132) or all 156; cap 300.
2. Tier targets: (A) one band per tier for both quantities, or (B) a pair (perfect-deal share, match %)?
3. SOFT constraints with no evidence are `negotiated` today and count as UNMET in match_original / perfect_deal, which depresses
   both for every candidate. Treat low-coverage SOFT like verification_only?
4. Interpretations in docs/decisions.md ("Interpretations to confirm") and `scenario.verification_only_fail_blocks` (false = a FAIL
   on a verification_only constraint is never scored).

## Known issues (not yet fixed)
- `accepted_relaxations` keeps the proposed option's `candidates_gained` for an accepted COUNTER (overstated); affects the
  candidates-gained metric. For BEST_AVAILABLE the negotiator_ranking is in offer order, so NDCG/tau compare the offer order.
- `turns_to_agreement` counts turns up to RECOMMEND even after a WRAP_UP.
- `generic_cert_terms` short entries (iso, isi, bis) match inside other words (prefilter too generous = extra LLM calls only).
- extract_evidence still treats any non-negated certification row as a claim when `doc_claims` is not passed.
- bid 209245: 4 constraints carry reframe_mismatch qa_flags (the model altered a value; they were left unchanged, as designed).

## Step 5: what was done (all committed)
| Item | Result |
|---|---|
| Coverage diagnosis, 6 bids (`docs/coverage.md`) | dataset flags do not discriminate HARD constraints; 60/136 HARD constraints have no evidence anywhere, 105 have <=5% |
| D043 | flag-based rule dropped, no new status |
| B: D046/D047 what-if (section 8) | negotiation triggers naturally in 0/6 bids under all four variants; conditional candidates have p 1e-6..1e-23 |
| A: live normalise coverage (`docs/coverage_llm.md`) | 45-46 HTTP requests of 60 (first run killed by a timeout wrapper with one request in flight, so one may have been billed twice); 92% null; mean HARD evidence 1.5->10.4% (242710), 8.7->9.9% (488257); zero clean candidates before/after |
| D045 | `refused` param (matching), transcript-only interpretation (barrier fix), counter-counter, no hardening |
| Golden negotiation test | 18 tests, exact values; proves the bundle/counter/BYPASS path works end to end |
| D044 | progressive reveal + CLARIFY + `Action.REVEAL` |
| matching fix | normalised values count without an alignment |

## Decisions waiting for the user
1. D046 / D047 (measured, not applied): see docs/coverage.md section 8.
2. How real bids reach negotiation at all (evidence is too sparse): scenario construction, or other bids/catalogues.
3. `doc_to_pending` is unreachable from a natural near-miss (golden-test finding): give it a rule or drop it from CLAUDE.md 3.4.
4. Should `reference_ranking` apply the buyer's accepted relaxations? Today it does not (D037 R3), so concessions the buyer made
   still show as SOFT FAIL in the truth ranking, although `Turn.buyer_truth_state_after` suggests the truth state evolves.
5. Doc-proof `claim` is true for any non-negated certification row, even a different standard (matching rule).
6. Live pilot (Q2) and the gt-auditor checklist stay on hold.

Open follow-ups: golden scenario is only tested with progressive reveal disabled; REVEAL turns do not consume patience;
mock-mode pilot output depends on `.cache/llm`.

Decision numbers: D043 assessability finding, D044 progressive reveal (+2 amendments), D045 refusals/counters/barrier,
D046/D047 proposals. D042 was already taken.

Caveats found on the way: (a) `pilot --mock` reads `.cache/llm` before fixtures, so its output depends on
earlier live runs on the same machine (tests do not); (b) the dataset flags `indiamart_matchable` /
`administrative_only` do not discriminate HARD constraints (all 139 HARD rows are matchable, none administrative).

## Completed

### Step 1 — Contracts and configuration
- `src/gtgen/contracts.py` — all models (D001–D013, D016, D025, D030, D033, D036)
- `config/settings.yaml` — all tunable constants through D032
- `CLAUDE.md` — spec complete through §7
- `.claude/agents/` — all 5 specialist agent files with ownership hooks
- `scripts/check_owner.py` — path enforcement matching §5, extended for `__main__.py`/`.env`/
  `pyproject.toml`/`requirements.txt`

### Step 2 — Data loaders and LLM client
- `src/gtgen/data/loaders.py` — 6 CSV loaders, canonical filtering, golden_param validation,
  CatalogueView/ScenarioBrief builders, profile(), parse-failure report (D019), parquet writer
  (D023, `gst_number` excluded). Pilot bid `GEM/2019/B/242710`: 216 candidates, 27 HARD/11 SOFT.
- `src/gtgen/llm/client.py` — OpenRouter client, disk cache keyed by sha256 = `llm_call_id`
  (D018), mock/record modes, backend selection (D017: `openrouter`/`gemini_direct`), D024
  rate pacing (min_interval_s), Retry-After handling, token budget, markdown-fence stripping.
- Model: `google/gemma-4-26b-a4b-it:free` (D022) — only served via `google-ai-studio` on
  OpenRouter; `gemini_direct` (deepinfra) 404s for this model.

### Step 3 — Matching, ranking, eval, LLM prompts v1
- `src/gtgen/matching/` — compliance (4 statuses), relaxation with D025 near-miss bundle
  search, D026 numeric "=" bands. **gt-auditor finding (`reports/review_matching.md`): refused-
  item exclusion is NOT implemented in relaxation.py — must be filtered by the caller
  (dialogue-engineer instructed to do this in negotiator.py).**
- `src/gtgen/ranking/matrix.py` — Bayesian rating, p_feasible, softmax ranking (D029).
- `src/gtgen/eval/` — metrics.py, conversations.py.
- 11 prompts v1 + `src/gtgen/llm/runners.py` (`LLMRunners`) + blocklist (D027, extended D032
  with "bid"/"bids"/"bidder"/"bidding"; 4 prompts reworded to avoid the word "bid").
- D033: `BuyerConstraint.framing_reason` persisted from reframe output.
- D036: `Persona.tau_band` stored; seeded-band persona generation gives even tier coverage.
- Pilot fixtures recorded live for GEM/2019/B/242710: reframe×2, doc_classify×2, persona×3
  (τ=0.384/0.618/0.914 across medium/medium_strict/strict), align×2, normalise×2; synthetic
  fixtures for the 6 dialogue-time prompts.
- `src/gtgen/cli.py` + `__main__.py`: `pilot --mock/--live --bid --personas N`, `eval --run`.
  `pip install -e .` makes `python -m gtgen.cli` and the `gtgen` console script work.

## Completed (continued)

### Step 4 — Dialogue engine (D042)
- `src/gtgen/dialogue/{buyer,negotiator,loop,assemble,_util}.py` + `tests/dialogue/` (36 tests).
  5th delegation attempt; first 4 failed on infra (Opus rate limit × 2, D040 hook-identity bug
  × 2 — see below), not design. All R1–R8 policy rulings implemented, including the corrected
  R5 (hardens a refused belief to HARD in place rather than filtering, which avoids a `KeyError`
  in `matching.compliance.feasible()`).
- `cli.py` fixture-cache-key fixes (D042): `product_category` now matches `record_pilot.py`'s
  most-common computation; `reframe`/`doc_classify`/`align` called with no explicit seed;
  `persona()`'s `scenario_id` now exactly `f"{bid_id}:s{seed}"`; `categories` list unnormalised
  to match `persona()`'s own internal dedup; a persona seed beyond what was recorded is skipped
  with a flag instead of crashing.
- **Infra blocker found and fixed (D040):** `check_owner.py` was trusting `argv` alone for the
  role, but the orchestrator's own frontmatter `PreToolUse` hook stays bound while any subagent
  runs and fires with its own literal `argv="orchestrator"` regardless of which subagent is
  actually executing. Fix: prefer the hook payload's `agent_type` field (set correctly by the
  harness inside a subagent) over `argv`. Verified via a `.hook_debug_on` debug-log control test
  before relying on it, confirmed again by the dialogue-engineer's successful 5th attempt
  (`check_owner.py` correctly named it `dialogue-engineer` this time).
- **Model policy (D038/D041):** all six `.claude/agents/*.md` files plus `.claude/settings.json`
  (`model`, `agent`, `env.CLAUDE_CODE_SUBAGENT_MODEL`) pin Sonnet everywhere. Coordinator never
  passes a model override on an Agent tool call.

## Open issues
| Issue | Owner |
|---|---|
| Refused-item exclusion missing in `matching/relaxation.py` (gt-auditor FAIL, `reports/review_matching.md`) | resolved in negotiator.py via R5's hardening approach (not the auditor's own suggested filter, which would crash); matching-engineer should still add a real `excluded` param later |
| Matching test coverage gaps (gt-auditor PARTIAL): SOFT UNKNOWN+SOFT FAIL combo, 4-item bundle, empty candidate list, doc_to_pending in a mixed bundle | matching-engineer (next) |
| No LLM prompt exists for RECOMMEND/NO_FEASIBLE_OUTCOME/CLARIFY negotiator utterances (only negotiator_reply, which requires a bundle) | deterministic templates in place for v1 (D039/D042); llm-engineer could add a prompt later if desired |
| `pilot --mock`'s dialogue-time turns (buyer_opening, reveal_check, negotiator_extract) fall back to `mock_stub:*` for this exact bid/persona input (no recorded fixture matches realistic free-form content) — negotiator recommends immediately from an empty belief set rather than exercising real bundle negotiation in this mock run | known v1 limitation (D042); the negotiation POLICY itself is separately verified by the dialogue-engineer's 36 tests using fake runners; live pilot (or a richer set of recorded dialogue fixtures) would exercise it fully |
| Ground-truth `reference_ranking` shows all 216 candidates `eligible=False` for this pilot bid — its HARD constraints are narrow ISI/IS-standard specs the catalogue mostly doesn't capture | data-coverage gap, not a new bug (consistent with earlier data-steward/llm-engineer findings on this bid); worth re-checking after a live `align`/`normalise` run, and worth weighing when choosing bid(s) for Step 7 scale |
| Provenance data-file hashes — `assemble_conversation` has no access to data file paths | deferred to v1 backlog (D039), not required |
| D034 (OEM authorisation doc_name = "authorised dealer certificate") not applied to the existing pilot fixture — it currently says "OEM Authorization Certificate", which is arguably fine; low priority | coordinator, cosmetic |
| `.env` holds the real `OPENROUTER_API_KEY` now (user-supplied); never commit it — confirmed gitignored | user |
| Free-tier rate limits (20 req/min) will bound pilot/scale throughput; live pilot estimated ~104 LLM calls / ~130 HTTP requests / ~7 min | coordinator, before any live run |

## Not started

### Step 6 — Pilot run and audit
- Mock pilot done (this update). Live pilot not yet run — awaiting the call-count estimate to
  be shown to the user first, per standing instruction.
- gt-auditor review of the generated conversations (barrier leaks, government terms,
  hard-constraint relaxation, unsupported claims, τ spread, reproducibility) — not yet run on
  actual dialogue output (the earlier `reports/review_matching.md` only covered the matching
  module's code, before the dialogue engine existed).

### Step 7 — Scale
- Full run across all 6 bids with constraints/products.
