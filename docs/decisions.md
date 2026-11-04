# Decisions

## D001 — golden_param mapping confirmed (2026-10-26)

`golden_param=True` → HARD, `golden_param=False` → SOFT. Confirmed by the user. OPEN QUESTION
note removed from CLAUDE.md §1.1.

## D002 — Split Persona into BuyerProfile + Persona (2026-10-26)

**Context:** CLAUDE.md §4 listed `buyer_profile` and `persona` as separate fields, but
`contracts.py` had a single `Persona` model that bundled company identity with policy parameters.

**Decision:** Split into two models:
- `BuyerProfile` — LLM-generated fictional company identity (company_name, industry, city, style).
  Visible in utterances.
- `Persona` — Behavioural parameters driving the deterministic buyer policy (tau, tier,
  category_tolerance, counter_band, patience_turns, ranking_weights).

`GTConversation` now has both `buyer_profile: BuyerProfile` and `persona: Persona`.

## D003 — Add ScenarioBrief model (2026-10-26)

**Context:** `GTConversation.commercial_brief` was `list[BuyerConstraint]`, missing quantity and
budget context needed for the opening message and price-based scoring.

**Decision:** New `ScenarioBrief` model wraps the constraint list with scenario_id, bid_ref,
quantity (from `gem_bids_metadata.quantity`), quantity_unit, and budget_inr (from a price/budget
constraint if present, otherwise None — `price_vs_budget` in the ranking row is then None).
`GTConversation.commercial_brief` is now `ScenarioBrief`.

## D004 — Add TranscriptView / TranscriptTurn (2026-10-26)

**Context:** The Negotiator's information barrier (CLAUDE.md §1.5) requires it to see only the
transcript and the catalogue. There was no typed contract enforcing this.

**Decision:** `TranscriptTurn` (turn_idx, speaker, utterance) and `TranscriptView` (scenario_id,
turns) are the only conversation types the Negotiator accepts. No hidden state, no proposals, no
constraint IDs.

## D005 — Add proposal_outcome and counter_value to Turn (2026-10-26)

**Context:** The GT record needed to capture what happened to each proposal without requiring
consumers to reconstruct it from subsequent turns.

**Decision:** `Turn.proposal_outcome: Optional[Literal["ACCEPTED","REJECTED","COUNTERED","BYPASSED"]]`
and `Turn.counter_value: Optional[str]` added. Populated on the turn that resolves the proposal.

## D006 — Add doc_name to BuyerConstraint (2026-10-26)

**Context:** `is_doc_proof` flagged whether a constraint needs a certificate/document, but the name
of the required document was not captured.

**Decision:** `BuyerConstraint.doc_name: Optional[str]` stores the document name (e.g. "ISO 9001
certificate"). Populated by the LLM verification-document classification step.

## D007 — Add pending_verification_docs to FinalOutcome (2026-10-26)

**Context:** §4 required an aggregated list of pending verification documents at the conversation
level. Only `RankingRow` had per-candidate lists.

**Decision:** `FinalOutcome.pending_verification_docs: list[str]` aggregates all unique pending
documents across all candidates in the final state.

## D008 — Wire ComplianceCell into FinalOutcome (2026-10-26)

**Context:** `ComplianceCell` was defined in contracts.py but never referenced by any parent model.
The ranking matrix only has aggregated counts, making it impossible to audit which constraints each
candidate passed/failed.

**Decision:** `FinalOutcome.compliance_detail_truth: list[ComplianceCell]` (from buyer truth state)
and `compliance_detail_belief: list[ComplianceCell]` (from negotiator belief state) provide full
per-candidate per-constraint audit detail.

## D009 — Add category to CandidateConstraint (2026-10-26)

**Context:** `product_constraints.csv` retains `category` as a broad constraint grouping. The
matching engine needs it to pair buyer and candidate constraints.

**Decision:** `CandidateConstraint.category: Optional[str]` added.

## D010 — New AttributeAlignment model (2026-10-26)

**Context:** The LLM table in §2 lists "Attribute alignment (buyer attribute ↔ product spec names),
per bid, cached" but no contract existed for this output.

**Decision:** `AttributeAlignment` model (bid_id, buyer_attribute, candidate_attribute, confidence,
llm_call_id). Per-bid, cached by the standard LLM cache key.

## D011 — New NormalisedValue model (2026-10-26)

**Context:** "Free-text value normalisation (brochure/description → value+unit)" had no contract.

**Decision:** `NormalisedValue` model (source_text, value, unit, confidence, llm_call_id). Used by
the compliance comparison step to compare buyer requirements against candidate evidence.

## D012 — Add condition to BuyerConstraint (2026-10-26)

**Context:** `final_constraints.csv` retains `condition` ("encodes the condition under which a
requirement applies"). Conditional constraints may need special handling in the buyer policy.

**Decision:** `BuyerConstraint.condition: Optional[str]` added. Loader preserves it; buyer policy
evaluates conditions before enforcing the constraint.

## D013 — Keep dual rankings, update §3/§4 (2026-10-26)

**Context:** §4 said `ranking_matrix` (singular), but the contract had `reference_ranking` +
`negotiator_ranking`. The dual approach is better for evaluation.

**Decision:** Keep both. §3 step 7 and §4 updated to list both rankings, PATIENCE_EXHAUSTED as a
third outcome, run_id, buyer_profile, and the compliance detail fields.

## D016 — Bundle relaxation proposals (2026-10-26)

**Context:** Single-option proposals lead to long negotiations when several soft constraints need
relaxing. Bundling multiple relaxation options in a single turn is more realistic and efficient.

**Decision:** New contracts and settings:
- `RelaxationBundle(items: list[RelaxationOption])` — multiple options proposed together.
- `Proposal.bundle: Optional[RelaxationBundle]` alongside existing `option` (now optional).
- `ItemResponse(constraint_ref, action, counter_value)` — buyer's per-item response to a bundle.
- `Turn.item_responses: list[ItemResponse]` — populated when responding to a bundle.
- `Action.MIXED` — buyer turn-level action when bundle items get different responses.
- `proposal_outcome` gains `"PARTIAL"` — mixed responses to a bundle.
- `Persona.max_concessions_per_turn: int` (default 2) — caps YES+BYPASS per turn.
- Settings: `negotiation.max_bundle_size: 4`, `negotiation.min_marginal_gain: 1`.

CLAUDE.md §3 steps 4–5 and §4 updated. matching-engineer and dialogue-engineer agent files updated.

## D017 — Selectable LLM backend (2026-10-26)

**Context:** The Google AI Studio BYOK route may be unavailable or rate-limited; we need a second
route to the same model with the same structured-output guarantees.

**Decision:** `llm.backend: openrouter | gemini_direct` in settings. Each backend under
`llm.backends.<name>.provider` holds the OpenRouter `provider` routing block sent with every call.
`openrouter` = AI Studio pin (`order: [google-ai-studio]`, no fallbacks). `gemini_direct` =
OpenRouter without the AI Studio pin: `order: [deepinfra]`, `allow_fallbacks: false`,
`require_parameters: true`. The top-level `llm.provider` key is removed. Unknown backend → error.

## D018 — llm_call_id is the cache key (2026-10-26)

**Decision:** `llm_call_id` = sha256 cache key over canonical JSON of (model, backend, provider
block, prompt_name, prompt_version, canonical inputs, seed). No UUIDs. Identical calls → identical
id and byte-identical cached response. Mock fixtures live at
`tests/fixtures/llm/<prompt_name>/<llm_call_id>.json`. Supersedes the key definition in CLAUDE.md §2
(which omitted backend/provider).

## D019 — JSON parse failures keep the raw string (2026-10-26)

**Context:** JSON-serialised cells (`allowed_values`, `specifications`, …) that fail to parse were
silently converted to None, losing evidence.

**Decision:** On parse failure the parsed field is None and the verbatim cell goes to a sibling
`*_raw` field: `BuyerConstraint.allowed_values_raw`, `CandidateConstraint.allowed_values_raw`,
`CandidateView.specifications_raw`. In canonical parquet the original column keeps the raw string.
Every failing row is listed in a parse-failure report (table, key, column, raw value, error).
Downstream must not interpret `*_raw` as a parsed list/dict; matching treats it as UNKNOWN-evidence
text only (may be passed to LLM normalisation).

## D020 — golden_param_true_means is mandatory (2026-10-26)

**Decision:** Missing or invalid (`not in {HARD, SOFT}`) `golden_param_true_means` raises an error at
load time. No default/fallback.

## D021 — Ownership map alignment (2026-10-26)

`scripts/check_owner.py` orchestrator paths narrowed from `config/` to `config/settings.yaml`;
CLAUDE.md §5 coordinator row now says `.claude/` (was `.claude/agents/`) and explicitly excludes
`docs/data_profile.md`, matching the hook.

## D022 — Model changed to google/gemma-4-26b-a4b-it:free (2026-10-26, user decision)

**Context:** Smoke test showed `google/gemma-4-31b-it` has no Google AI Studio endpoint on
OpenRouter (404 with the AI Studio pin). User chose `google/gemma-4-26b-a4b-it:free`.

**Decision:** `llm.model: google/gemma-4-26b-a4b-it:free`. OpenRouter's endpoint list for this model
(checked 2026-10-26) has exactly one endpoint: Google AI Studio (`google-ai-studio`), which supports
`response_format`. Default backend stays `openrouter` (AI Studio pin). `gemini_direct` (DeepInfra)
does not serve this model and will 404 while this model is selected. Changing the model changes
every llm_call_id (D018), so the cache and recorded fixtures are per-model.

## D023 — gst_number excluded from canonical parquet (2026-10-26)

`write_canonical` omits `gst_number` from `data/canonical/suppliers.parquet`: CLAUDE.md §7 says it never
leaves `src/gtgen/data/`. If identity reconciliation ever needs it, data-steward adds an in-package
loader, not a file.

## D024 — Rate-limit, token-budget and retry settings for the free model (2026-10-26, user input)

**Context:** User reports `google/gemma-4-26b-a4b-it:free` allows ~10k tokens per call and 20 requests
per minute. The client had hardcoded backoff (1, 4, 16 s), no pacing, and 8000 completion tokens.
Smoke test also showed AI Studio wraps json_schema output in ```json fences.

**Decision (settings `llm`):** `max_tokens_per_call: 10000` (prompt+completion), `max_completion_tokens:
6000`, `min_completion_tokens: 1000`; `rate_limit.min_interval_s: 3.2` enforced process-wide before
every request (validation retries included); `http_retry`: 4 retries on 429/5xx/timeouts, backoff
[5, 15, 30, 60] s, honour `Retry-After` up to 90 s, beyond that fail fast (daily quota). Client strips a
single surrounding markdown code fence before pydantic validation; anything else still goes through
the validate-retry-qa_flag path. Backoff constants move from code to settings.

## D025 — Near-miss bundle search replaces greedy bundle selection (2026-10-26, user decision)

**Context:** Greedy `select_bundle` (D016) skips options whose individual gain < `min_marginal_gain`,
so it can never find a bundle where no single relaxation helps but a pair does.

**Decision:** Near-miss candidates = infeasible only because of ≤ `max_bundle_size` SOFT FAILs (any
HARD FAIL/UNKNOWN excludes; SOFT UNKNOWN does not block feasibility). Enumerate subsets of their
soft blockers (≤ 4); each item relaxed to the loosest level needed by near-miss candidates whose
blocker set ⊆ the subset. Keep minimal bundles (removing any item drops joint gain by
≥ `min_marginal_gain`). Rank: joint gain desc → fewer items → lower total normalised cost → sorted
refs. Contract: `RelaxationBundle` gains `joint_gain`, `feasible_after`, `gained_product_ids`,
`total_normalised_cost` (defaults, backward compatible). Settings: `negotiation.bundle_search`.

## D026 — Relaxing numeric "=" constraints (2026-10-26)

**Context:** Pilot bid's numeric SOFT constraints (`max_current = 4.5 A`, `motor_rating = 1.5 HP`) use
`=`; widening had no direction.

**Decision:** `allows_higher=True` → treat as `>=`. Otherwise relaxation is a symmetric band
`[t−d, t+d]` with d = distance to the admitted candidate value; `to_value = "lo..hi"`; cost `d/|t|`
capped to 1. Equality uses relative tolerance `negotiation.bundle_search.numeric_eq_rel_tolerance`.

## D027 — Government-term blocklist lives in settings (2026-10-26)

`framing.government_terms` in settings is the single list used by llm-engineer output checks and by
gt-auditor. Case-insensitive whole-word/phrase match.

## D028 — Temperatures keyed by prompt name; Step 3 LLM scope (2026-10-26)

`llm.temperature` keys are exactly the 11 prompt names (reframe, doc_classify, persona,
buyer_opening, buyer_reply, negotiator_extract, negotiator_reply, align, normalise, reveal_check,
claim_check). `buyer_utterance`/`negotiator_utterance` removed. A prompt without a key → error.
Pilot fixtures for GEM/2019/B/242710 are recorded live for pre-dialogue prompts only (reframe,
doc_classify, persona ×3 seeds, align, normalise) on backend `openrouter` / gemma-4-26b-a4b-it:free,
cap 60 live calls; dialogue-time prompts get synthetic fixtures until Step 4.

## D029 — Rulings on matching-engineer Step 3 questions (2026-10-26)

Checked against data/canonical/:
1. `suppliers.member_since` is "N yrs" in 100% of rows (e.g. "10 yrs", "2 yrs"; null rate 0). Tenure =
   N parsed directly; no clock, no `as_of`. Unparseable → None (tenure feature 0, qa note). The
   4-digit-year parser was wrong for all real data → fix assigned to matching-engineer.
2. `ScenarioBrief.budget_inr` is the **total** budget for the whole quantity:
   `price_vs_budget = price × quantity / budget_inr`. Pilot bid has no budget constraint → None (D003).
3. `Persona.category_tolerance` is keyed by `BuyerConstraint.constraint_category` (pilot values:
   CAPACITY, COMMERCIAL, DELIVERY, DOCUMENT, PRODUCT, QUALITY, SUPPLIER); missing key → 1.0. The
   persona prompt must emit exactly these keys (llm-engineer, next prompt).
4. Settings `ranking.rating_max: 5.0`, `ranking.tenure_cap_years: 10` added; code must read them,
   no in-code fallback.
5. Folding the 0.9 unverified factor into `ComplianceCell.evidence_confidence` is accepted; no
   contract change.
6. Barrier-leak count and unsupported-claim rate belong to gt-auditor, not eval/.

## D030 — NegotiatorUnderTest protocol (2026-10-26, user decision)

(The user asked for this as "D017"; D017 was already taken by the LLM backend setting, so it is D030.)

**Decision:** `contracts.py` adds `NegotiatorStep` (beliefs: list[BelievedConstraint], action,
proposal: Optional[Proposal], ranking: Optional[list[RankingRow]], utterance, llm_call_ids) and a
`runtime_checkable` Protocol `NegotiatorUnderTest.step(transcript: TranscriptView, catalogue:
CatalogueView) -> NegotiatorStep`. Validation: action ∈ {CLARIFY, PROPOSE_RELAXATION, RECOMMEND,
NO_FEASIBLE_OUTCOME}; proposal set iff PROPOSE_RELAXATION (proposer NEGOTIATOR); ranking set iff
RECOMMEND. The GT Negotiator (dialogue-engineer, Step 4) must implement it. Eval will run any
implementation in **replay** mode (fed GT transcript prefixes turn by turn, compared with GT
negotiator turns) and **live** mode (against BuyerSimulator with the same scenario seed). The
signature itself enforces the information barrier: no other inputs.

## D031 — Restore of coordinator files after 21:12:15 regression (2026-10-26)

**Context:** `config/settings.yaml` and `src/gtgen/contracts.py` were rewritten 1 ms apart at
21:12:15 with older content (lost D025/D026/D027/D028/D029 settings and D025/D030 contract types).
Only the main session edits these files; most likely a conversation rewind restoring checkpoints,
coinciding with the D024 settings edit. Suite went red (4 failed → 78 failed with llm tests).

**Ruling:** Restored to the recorded decisions, using key names the code reads (relaxation.py
`params_from_settings`, ranking/matrix.py `_REQUIRED`): `llm.temperature` per prompt name (D028),
`negotiation.bundle_search` {max_blockers 4, categorical_extend_cost 0.5, drop_cost 1.0,
numeric_eq_rel_tolerance 1e-6} (D025/D026, = code defaults), `ranking.rating_max 5.0`,
`tenure_cap_years 10` (D029), `framing.government_terms` (D027, list as reconstructed by
llm-engineer); `RelaxationBundle` D025 fields; `NegotiatorStep` + `NegotiatorUnderTest` (D030).
D024 rate-limit block kept. Pre-restore copies kept in the session scratchpad. Result: 224 passed.
Mitigation: commit after each integration now that the repo is under git.

## D032 — Add "bid" and inflections to government-term blocklist (2026-10-27)

`framing.government_terms` gains: `bid`, `bids`, `bidder`, `bidding` (whole-word match).
Existing `bid number`, `bid security`, `bid end date` stay for phrase matching.

## D033 — BuyerConstraint.framing_reason (2026-10-27)

`framing_reason: Optional[str]` stores the LLM-generated reason for keep/translate/drop.
llm-engineer must persist it from the reframe output and re-record affected fixtures.

## D034 — OEM authorisation kept as translate + is_doc_proof (2026-10-27)

The reframe step's translation of OEM authorisation ("Supplier to provide OEM Authorization
Certificate") is accepted. Additionally mark `is_doc_proof=True`,
`doc_name="authorised dealer certificate"`. Hardness follows `golden_param` as always.

## D035 — Commit after every green integration (2026-10-27)

`.gitignore` confirmed to cover `data/raw/`, `.env`, `.cache/`, `out/`, `.venv/`.
Coordinator commits after every integration where `pytest -q` passes.

## D036 — Tau bands: seeded RNG stratification across tiers (2026-10-27)

Each scenario's persona seed is used to assign a tau band `[lo, hi)` from the four tier
boundaries, stratified so the tiers are covered evenly across the run (round-robin by
`seed % 4` over tiers). The LLM picks tau within the band. `Persona.tau_band:
Optional[tuple[float, float]]` stores the assigned band; also recorded in `provenance`.
`tau_band=None` for free choice (legacy/testing).

## D037 — Dialogue-engine design rulings (2026-10-28)

**Context:** First dialogue-engineer attempt (non-worktree, hook ran as "orchestrator" — see
below) found real signature mismatches in the Step 4 task prompt and 6 open design questions
before writing any code. Re-verified every signature against source; rulings below unblock the
retry.

**Hook-identity bug:** without `isolation: "worktree"` on the Agent call, `check_owner.py` runs
with the orchestrator's own identity rather than the target agent's, blocking all subagent
writes outside orchestrator's paths. Workaround: always set `isolation: "worktree"` for
dialogue-engineer (and any agent) tasks. Not a contract issue; a harness behaviour to route
around.

**R1 (alignment barrier):** `AttributeAlignment` list computed once per bid (truth-side,
`runners.align(bid_id, constraints, candidate_constraints)`) is passed into `Negotiator`'s
constructor (not `step()`, so the `NegotiatorUnderTest` protocol is untouched) and converted to
`{buyer_attribute: alignment.model_copy(update={"bid_id": ""})}` internally. Safe because
`compliance_matrix`/`select_bundle`/`nearest_gain_value` are pure Python (no LLM call ever
serialises an `AttributeAlignment`); the `bid_id` strip is defense-in-depth only.

**R2 (belief→truth mapping):** done in `loop.py`, not negotiator.py/buyer.py. Exact normalised-
attribute match, then token-overlap ≥ 0.5 fallback; stamps `BelievedConstraint.matched_
constraint_id` (contracts.py: "filled by GT assembler only, never shown to negotiator"). Produces
`resolved: dict[belief_id, Optional[BuyerConstraint]]` passed to `buyer.respond_to_bundle`.
Unmatched belief → buyer answers YES, qa_flag `unmatched_belief:<ref>`, excluded from the
concession cap.

**R3 (truth state is static):** confirmed intended — `brief.constraints` never mutates across
turns; accepted relaxations live only in `accepted_relaxations`/provenance, never in
`reference_ranking`. Matches CLAUDE.md step 7's two-track reference/negotiator ranking design.

**R4 (COUNTER only for numeric items):** categorical/drop/doc_to_pending items that would
otherwise qualify for COUNTER by cost get NO instead — no well-defined partial value exists for
them.

**R5 (refused-item exclusion workaround for the missing `excluded` param in `select_bundle`):**
`Negotiator._refused_attrs: set[str]`, populated via `mark_refused(attribute)` from the loop after
every buyer NO. Filtered out of the belief list before calling `select_bundle`. Matching-engineer
should still add a real `excluded` parameter later (gt-auditor FAIL finding, `reports/
review_matching.md`) — this is a stopgap, not a fix to the owning module.

**R6 (quantity/budget for negotiator-side ranking):** the Negotiator extracts quantity/budget
from the `TranscriptView` text via regex (the buyer's opening legitimately states them) rather
than needing a contract change — this is the correct barrier-respecting design, not a hack.
`buyer_city` defaults to None on the negotiator side (no location signal via the barrier).

## D038 — All subagents and orchestrator switched to Sonnet 5 (2026-10-28, user decision)

**Context:** Opus-based subagent calls (orchestrator, dialogue-engineer, gt-auditor) hit a
session-wide rate limit twice during Step 4 delegation ("You've hit your session limit", HTTP
429, model `claude-opus-4-6`), stalling the dialogue-engineer task three times. User confirmed
directly: "change all models to sonnet 5 for all md agents."

**Decision:** `model: sonnet` in all six `.claude/agents/*.md` files (orchestrator,
dialogue-engineer, gt-auditor — previously `opus`; data-steward, llm-engineer,
matching-engineer — already `sonnet`). Bare `sonnet` resolves to the newest Sonnet, which is
Sonnet 5 as of this session. Frontmatter YAML re-validated after the edit.

## D039 — Dialogue-engine ruling refinements (2026-10-28)

**Context:** 4th dialogue-engineer attempt (a4c43442efadda007) again hit a hook-identity bug
("ran as orchestrator" despite `subagent_type: dialogue-engineer` + `isolation: worktree`) and
wrote no code, but it read the full codebase and surfaced real refinements to D037's rulings
before stopping (correctly, without attempting a workaround).

**R7 seed, corrected:** `hash(scenario_id)` is per-process randomised (`PYTHONHASHSEED`), which
would break warm-cache byte-identical reruns. Use
`int(hashlib.sha256(scenario_id.encode()).hexdigest()[:8], 16) % (2**31)` instead.

**R1, refined:** `alignments` (D037/R1) is keyed by the TRUTH attribute name
(`AttributeAlignment.buyer_attribute`), but the Negotiator's own `BelievedConstraint.attribute`
values come from LLM extraction and will often use different wording. An exact
`alignments.get(belief.attribute)` lookup would rarely hit. Fix: inside `negotiator.py`, map each
belief to the correct alignment entry using the SAME matching rule as R2 (normalised-exact match,
then token-overlap fallback) but applied only against the R1 alignment dict's keys — producing a
per-belief alignment dict to hand to `compliance_matrix`. This stays within the barrier (pure
string matching on attribute names already visible to the negotiator via its own beliefs; no new
LLM exposure).

**Token-overlap ratio (R2), confirmed:** `|A ∩ B| / max(|A|, |B|)`, tokens split on
`[^a-z0-9]+` (not just `_`), threshold ≥ 0.5.

**Accepted minor additions (approved, not required):** an `item_labels: dict[str,str]` kwarg on
`BuyerSimulator.respond_to_bundle` so buyer utterances can name an unmatched-belief attribute
instead of a bare belief_id; dropping individual catalogue evidence snippets that fail
`assert_negotiator_safe` before they reach `negotiator_reply`, rather than letting one bad
snippet crash the whole call.

**Deferred (not required for v1):** provenance data-file hashes (`assemble_conversation` has no
access to `data/raw`/`data/canonical` paths) — skip for v1, backlog as a future `cli.py` +
`assemble.py` wiring task, not a blocker.

**Open infrastructure question (unresolved):** every dialogue-engineer delegation attempted
after the mid-session `/model sonnet-5` switch has hit a hook-identity bug (the PreToolUse hook
fires with role `orchestrator` instead of the spawned subagent's own role), on both default and
`isolation: "worktree"` Agent calls. Every delegation attempted *before* that switch (data-steward,
llm-engineer ×2, matching-engineer, gt-auditor) correctly received its own role. Running a control
test (gt-auditor, trivial write) post-switch to determine whether this is a general regression or
specific to repeated dialogue-engineer invocations.

## D040 — Hook-identity regression: root cause and fix (2026-10-28)

**Confirmed general, not dialogue-engineer-specific.** A trivial gt-auditor control task (write
one line to `reports/`) hit the identical bug: `check_owner.py` ran with role `orchestrator`
instead of `gt-auditor`.

**Root cause (user diagnosis, confirmed by debug log):** the orchestrator runs as the main thread
(`--agent orchestrator`), so its own frontmatter `PreToolUse` hook stays bound and fires on
subagent writes too, passing its own literal `argv[1]="orchestrator"` regardless of which
subagent is actually running. `check_owner.py` was trusting `argv` alone.

**Fix:** `check_owner.py` now resolves the role as
`payload.get("agent_type") or argv[1] or "orchestrator"`. Claude Code's hook payload includes
`agent_type`, set to the real subagent's type inside a subagent, which takes precedence over
`argv`. Verified via a debug switch (`.hook_debug_on` → appends `{argv, agent_type, agent_id,
path, resolved}` per decision to `.hook_debug.log`; both gitignored). Control-test log line:
```
{"argv": ["orchestrator"], "agent_type": "gt-auditor", "agent_id": "a534e7e7dce48cfa3",
 "path": ".../reports/hook_check.md", "resolved": "gt-auditor"}
```
`argv` said orchestrator; `agent_type` correctly said gt-auditor; the fix resolved to
`gt-auditor` and the write succeeded. Confirms: `check_owner.py`'s own matching logic was never
the problem, and no workaround/loosening was needed — the real fix was reading the field the
harness already provides but the script wasn't using.

**Scope note:** the worktree-prefix fix (stripping `.claude/worktrees/<id>/` before matching,
recorded alongside this fix) remains in place and still passes its own regression tests; the two
fixes are independent and both needed.

## D041 — Session-level model defaults added (2026-10-28, user command)

**Context:** D038 set `model: sonnet` per-agent in `.claude/agents/*.md`. User additionally
pinned session-level defaults so the main thread and any built-in subagent also default to
Sonnet, independent of per-agent frontmatter.

**Decision:** New `.claude/settings.json`:
```json
{"model": "sonnet", "agent": "orchestrator", "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}}
```
`model` = default model for the main session; `agent` = default agent when none is specified;
`env.CLAUDE_CODE_SUBAGENT_MODEL` = default for the harness's own built-in subagent types (not
the six `.claude/agents/*.md` custom agents, which already carry their own `model:` field per
D038). All six agent files re-validated as parseable YAML frontmatter with `model: sonnet`.
`pytest -q`: 250 passed (unaffected, as expected).

**Policy (user, 2026-10-28):** every session and subagent runs on Sonnet via the three
mechanisms above. The coordinator never passes a `model` override on an Agent tool call —
each delegation inherits Sonnet from its agent file's own frontmatter.

## D042 — Dialogue engine landed; cli.py fixture-cache-key fixes; pilot --mock runs (2026-10-28)

**Dialogue engine (5th attempt, ad2767b40dff9efb6) succeeded** after the D040 hook-identity
fix. `src/gtgen/dialogue/{buyer,negotiator,loop,assemble}.py` + `_util.py`, `tests/dialogue/`
(36 new tests). Merged into main checkout. Full suite: 286 passed (250 prior + 36 new).

**Accepted departures from the R1-R8 spec (all sound, approved as-is):**
- `mark_refused` receives the negotiator's own wording of the attribute (not the truth
  attribute name) — correctly keeps truth-side vocabulary off the negotiator's side of the
  barrier; R5's hardening match is an exact normalised match on that wording.
- Empty beliefs (e.g. from a mock-stub negotiator_extract) are explicitly handled so
  `feasible_product_ids` doesn't wrongly return `NO_FEASIBLE_OUTCOME` — confirmed intentional
  (CLAUDE.md's feasibility definition is "no HARD FAIL/UNKNOWN, no SOFT FAIL"; zero
  constraints trivially satisfies this for every candidate).
- R2 matching also tries `spec_name` after `attribute` fails, truth-side only.
- Highest-confidence alignment wins on a multi-match; ties keep the first.
- R6 regexes widened slightly (word boundaries, "budget of/is/around/up to", lakh/crore).
- A COUNTER requires the interpolated fraction strictly between 0 and 1, else NO.
- Buyer LLM seed: `persona_seed` at opening, `persona_seed + turn_idx` after.
- `reveal_check` runs only on the opening turn.
- qa_flags not deduplicated.
- An unhandled negotiator action (e.g. a swapped-in `NegotiatorUnderTest` returning CLARIFY,
  which this v1 GT negotiator never emits) is flagged `unhandled_negotiator_action`; the loop
  continues up to the turn cap rather than crashing — matters for eval's "live mode against
  an arbitrary implementation" (D030).

**cli.py fixture-cache-key fixes (coordinator, own file).** `pilot --mock` initially failed/
mismatched because three of `cli.py`'s pre-dialogue calls didn't reproduce the exact inputs
`record_pilot.py` used when recording the pilot fixtures (cache key = sha256 of canonical
inputs, D018 — any difference misses the cache):
1. `product_category` must be the catalogue's *most common* `category_name`
   (`collections.Counter(...).most_common(1)`), not the first candidate's — now fixed.
2. `reframe`/`doc_classify`/`align` must be called with no explicit `seed` (record_pilot.py
   never passes one) — the earlier `seed=42` was wrong and has been removed.
3. `persona()`'s `scenario_id` must be exactly `f"{bid_id}:s{seed}"` (record_pilot.py's
   format) — not `cli.py`'s own readable `"GEM_2019_B_242710_s{seed}"`, since `scenario_id`
   feeds `persona()`'s cache-key nonce. (Confirmed harmless for every other prompt — none of
   reframe/doc_classify/align/normalise/negotiator_extract/negotiator_reply take `scenario_id`
   as an input at all, so `ScenarioBrief`/`CatalogueView`/`TranscriptView.scenario_id` can stay
   human-readable without affecting any cache key.)
4. `persona()`'s `categories` list must mirror `record_pilot.py`'s raw (non-deduped,
   non-GENERAL-substituted) list — `persona()` itself does the dedup/sort/falsy-filter
   internally, so passing a pre-normalised list with `None` silently coerced to `"GENERAL"`
   would produce a different final category set than passing the raw list through.
5. A missing persona fixture (seed > `personas_per_bid`, i.e. beyond what was ever recorded)
   now logs a warning, flags `mock_fixture_missing:persona:seed{n}`, and skips that persona
   instead of crashing the whole run (CLAUDE.md §2: never fabricate, skip and flag).

**Result:** `pytest -q` → 286 passed. `python -m gtgen.cli pilot --mock --bid
GEM/2019/B/242710 --personas 4` → runs end-to-end, writes 3 valid `GTConversation` records
(seed 4 skipped, flagged, no crash), golden_param/framing_reason/provenance all correctly
populated. The reframe/doc_classify/align/persona fixtures now hit the real recorded cache
(persona names and τ match the pilot recording exactly); the free-form dialogue-time prompts
(buyer_opening, reveal_check, negotiator_extract) still fall back to `mock_stub:*` because no
recorded fixture matches this exact realistic input — by design (CLAUDE.md: a missing
dialogue fixture returns a stub, never crashes). Consequence: the negotiator recommends
immediately from an empty belief set (216/216 trivially "feasible" with zero believed
constraints) rather than exercising real bundle negotiation in this *mock* run. The policy
logic itself (bundle MIXED/PARTIAL, HARD→NO, doc-proof→BYPASS, barrier enforcement) is
separately verified by the dialogue-engineer's 36 tests using fake runners, not by this
particular fixture-dependent pilot run.

**Reference ranking observation (not a bug):** the ground-truth `reference_ranking` shows
every one of the 216 candidates `eligible=False` (`hard_unknown` 26-27 of ~27 HARD
constraints). This bid's HARD constraints are narrow technical/ISI-standard specs
(`motor_standard`, `standard_conformity`, etc.) that the IndiaMART catalogue's attribute
coverage doesn't capture for most candidates — consistent with the data-steward's and
llm-engineer's earlier findings about this specific pilot bid's alignment difficulty, not a
new defect. Worth a live-pilot comparison (live `align`/`normalise` may recover more matches
than the already-recorded mock fixtures) and worth keeping in mind when choosing which bid(s)
to scale to.

**gt-auditor's FAIL (refused-item exclusion) resolved via R5's hardening approach, not its
own suggested fix.** `reports/review_matching.md` recommended filtering refused refs out
before calling `select_bundle`; R5 instead hardens the refused belief to HARD in place
(filtering causes a `KeyError` in `matching.compliance.feasible()`, confirmed by two earlier
failed attempts) — same effect (never re-proposed, CLAUDE.md §1.6-compliant), no crash risk.
matching-engineer's own `excluded` parameter (the auditor's other suggestion) remains a
backlog item, not required.

## D043 — Flag-based assessability dropped; finding recorded, no new status (2026-10-29, user decision)

**Finding (docs/coverage.md, verified by the coordinator):** `indiamart_matchable` and `administrative_only`
do not discriminate HARD constraints: all 139 HARD rows are `indiamart_matchable=True` and none is
`administrative_only` (the only two such rows are SOFT transaction rows). A flag-based NOT_ASSESSABLE rule
touches zero HARD constraints; feasible counts under current rules and under the flag rule are 0 in all 6 bids.
The cause is catalogue evidence sparsity, not flags and not the alignment step: of 136 active HARD constraints,
60 have no evidence on any candidate and 105 have <=5% (cells computed without `normalise`, so an upper bound on
UNKNOWN). Exact attribute-name overlap with candidate evidence is 1-8% of products; `product_constraints.constraint_id`
has 39 category-level values with no overlap with buyer constraint ids. Coverage-threshold NOT_ASSESSABLE
(theta=0.25) would make only bids 242710 (135, with all 27 HARD constraints unassessable) and 488257 (2) feasible.

**Decision:** no new compliance status; CLAUDE.md 1.6/1.7 unchanged (UNKNOWN on HARD is never PASS). The replacement
is chosen after two measurements: (A) real align+normalise coverage on a candidate sample (docs/coverage_llm.md) and
(B) the effect of D046/D047 on all 6 bids (docs/coverage.md section 8). Live pilot on hold (user, Q2).

**Decisions of the same date:** matching-engineer adds a `refused` parameter to the relaxation search (D045);
dialogue-engineer passes it in. Golden-scenario test proceeds independently of this decision.

## D044 — Progressive reveal (2026-10-29, user decision; design details by coordinator)

The buyer's opening states at most 5-7 constraints; the rest are revealed later. Chosen in code, not by the LLM:
`N = opening_min + persona_seed % (opening_max - opening_min + 1)`; order = HARD first, then "key specs"
(`matchability == product`), then `category_priority`, then original order. Settings: `dialogue.progressive_reveal`.
Later reveals happen (a) when the negotiator asks (a CLARIFY turn; the negotiator asks before its first RECOMMEND,
up to `clarify_max_rounds`) and (b) when any negotiator utterance, including a proposal, mentions an unrevealed
constraint. Reveal matching is deterministic code over the utterance text (attribute/spec/commercial_text tokens);
an unspecific question reveals the next batch by priority (at most `reveal_max_per_answer`). `reveal_check` still
verifies what was actually said; `Turn.revealed_constraint_ids` records it. The negotiator learns only what is said,
so truth-side and belief-side feasibility legitimately diverge (that gap is what the eval measures).
New prompts (llm-engineer): `negotiator_clarify` (transcript + catalogue only), `buyer_reveal`.
Temperature keys added for every new prompt (D028 rule). No contract change (`Action.CLARIFY` and
`Turn.revealed_constraint_ids` already exist).

## D045 — Refusals, counters and the barrier (2026-10-29, user decision; barrier fix by coordinator)

1. **No hardening.** `believed_hardness` is never changed by a refusal (supersedes R5 of D037).
2. **`refused` parameter (matching-engineer):** `search`, `search_bundles`, `select_bundle` and `nearest_gain_value`
   accept `refused: Mapping[constraint_ref, Sequence[RelaxationOption]]`. An option for that ref is excluded when
   its level is equal to or looser than any refused option; strictly tighter levels stay available.
3. **Barrier fix.** The D042 loop told the negotiator which items were refused through `mark_refused()`, i.e. from
   the buyer's hidden decision. That is a side channel the transcript does not carry and an arbitrary
   `NegotiatorUnderTest` would not get (rule 1.5; it would also make the GT negotiator an unfair baseline in replay
   eval). Replacement: a negotiator-side prompt `negotiator_interpret` classifies the buyer's reply to the
   negotiator's own last proposal, per item: ACCEPT / REFUSE / COUNTER(value) / UNCLEAR. The loop passes nothing but
   `step(transcript, catalogue)`. The negotiator keeps `refused_values` and accepted relaxations as its own state
   (so `step()` must be fed consecutive prefixes of one conversation).
4. **Accepted items** are applied to beliefs deterministically (numeric: value := to_value, band -> operator range;
   categorical: operator in; drop: remove; doc_to_pending: is_doc_proof) instead of relying on re-extraction.
5. **COUNTER:** if the buyer's counter value already gains >= `negotiator_counter_requires_gain` candidates the
   negotiator accepts it; otherwise it counter-counters with `nearest_gain_value` as a 1-item bundle, which the buyer
   evaluates with the same item policy. (Reverses R8 of D039, which deferred this.)

## D046 / D047 — proposals under evaluation, NOT applied (2026-10-29)

D046 grouping (merge clauses of one standard into one doc-proof constraint, framing_decision "merge") and D047
conditional eligibility (no HARD FAIL + some HARD UNKNOWN -> conditionally eligible, p_c = smoothed pass rate with
prior 0.5 / pseudo-count 2, outcome RECOMMENDED_CONDITIONAL) are measured on all 6 bids in docs/coverage.md
section 8 before the user decides. Note found while scoping D046: `requirement_group_id` is a whole-product-line
grouping (18 groups over 178 rows), not a standard-clause grouping, so another grouping key must be measured.

## D044 amendment — Action.REVEAL (2026-10-29)

The buyer turn that answers a CLARIFY question or reveals touched constraints is labelled
`Action.REVEAL` (new enum member, backward compatible). Reusing `CLARIFY` would make a buyer turn
indistinguishable from a negotiator turn for anyone reading `action` alone. `NegotiatorStep` validation is
unchanged (negotiator actions: CLARIFY, PROPOSE_RELAXATION, RECOMMEND, NO_FEASIBLE_OUTCOME). Affected owners:
dialogue-engineer (emits it), matching-engineer (eval/conversations.py must not flag a reveal turn as a policy
violation).

## D044 amendment 2 — key specs by constraint_type (2026-10-29)
`BuyerConstraint` carries no `matchability`; `scope` is looser (9 supplier-matchable rows have product scope).
"Key specs" are therefore `constraint_type in dialogue.progressive_reveal.key_spec_types`
(`product_spec`, `capacity`, `quality`: 122 + 6 + 4 product-matchable rows in the data). Replaces
`key_spec_matchability`.

## D046 — Grouping ADOPTED, narrowed (2026-10-29, user decision)
Only clause-conformity members with NO measurable value are merged into one doc-proof constraint
("conforms to IS xxxx" with `doc_name`, e.g. "IS xxxx conformity certificate"). Members that carry a value stay
separate. `framing_decision = "merge"` on the members, the merged constraint carries `merged_from` (original ids kept).
Hardness rules stay per member, so a SOFT FAIL is never promoted to HARD. *Coordinator's reading:* a group is
formed per (standard id, hardness): only members of the same hardness merge, so nothing is ever promoted; the
standard id is extracted by regex from raw_text / buyer_value / requirement_description (as in docs/coverage.md
section 8); "no measurable value" = no numeric value-with-unit and no numeric operator in the member. Settings:
`scenario.merge_clause_conformity`, `scenario.merge_min_members`.

## D047 — Conditional eligibility DROPPED (2026-10-29, user decision)
Not used: the product of per-constraint probabilities for HARD UNKNOWN is ~0 (docs/coverage.md section 8).
`RECOMMENDED_CONDITIONAL` is not introduced; rule 1.6 is unchanged for negotiated constraints.

## D048 — Evidence-grounded brief: scenario_role (2026-10-29, user decision)
Each non-government constraint gets a `scenario_role` and an `evidence_coverage` (both stored on `BuyerConstraint`):
- `negotiated`: evidence coverage in the bid's pool >= `scenario.negotiated_min_coverage` (0.25), computed from the
  catalogue plus cached LLM evidence. Scored; rule 1.6 holds in full (HARD UNKNOWN excludes the candidate).
- `verification_only`: any other HARD constraint. Still voiced by the buyer; NOT scored; listed in
  `RankingRow.verification_required` for every recommended candidate; its `ComplianceCell.scored` is False.
- `dropped`: government-only (reframe step).
*Coverage definition (coordinator):* share of the pool's candidates whose status for the constraint is not UNKNOWN,
counting structured evidence for every candidate and grounded normalised values only where a cached probe exists
(today: the 20 sampled candidates of bids 242710 and 488257). A lower bound; recorded in `ScenarioBrief.scenario_meta`.
*SOFT constraints (coordinator):* SOFT UNKNOWN never blocks, so SOFT constraints are `negotiated` whatever their
coverage; their coverage is still stored.

## D049 — Synthetic commercial SOFT terms (2026-10-29, user decision)
Simulator-defined, provenance `synthetic_commercial`, SOFT, `golden_param = None` (NOT False: golden_param is a data
field and is never invented; the contract validator enforces it): `budget_inr` (total price x quantity <= value),
`qty_vs_moq` (moq <= quantity x (1 + slack)), `min_supplier_rating`, `min_tenure_years`, `preferred_states`.
Evidence comes from catalogue fields (price, moq, rating, member_since, state), so they always have coverage.
Quantity comes from the bid metadata. Code derives numeric bands from the pool's distribution
(`scenario.commercial_terms.band_percentiles`); the persona LLM picks the values inside the bands; a per-scenario
`tightness_target` (0..2, below min_shortlist) is chosen in code. *Coordinator's reading:* after the LLM picks, code
computes the actual initial verified-feasible count; if it is >= min_shortlist, code tightens deterministically up to
`max_tightening_steps` and records target, actual count and steps in `ScenarioBrief.scenario_meta`. A term whose band
is degenerate (no variation in the pool) is omitted and the omission recorded. `ScenarioBrief.budget_inr` is set
from the chosen budget so `price_vs_budget` works. The persona's raw picks are kept in `Persona.commercial_terms`.

## D051 — Doc-proof constraints, claims, doc_to_pending (2026-10-29, user decision)
- Claim detection: LLM extraction returns a claim with a QUOTE; code checks that the quote occurs in the listing text
  (case/whitespace-normalised). An unmatched quote counts as no claim (`DocClaim`, `doc_claims.require_verified_quote`).
- A claim present -> PENDING_VERIFICATION automatically (unchanged, not penalised).
- SOFT doc-proof with no claim: the candidate is blocked; the negotiator may offer `doc_to_pending`; the buyer answers
  BYPASS (candidate stays, doc listed, score unchanged). *Representation (coordinator):* status FAIL with evidence
  "no_claim" so the existing near-miss machinery applies (SOFT FAIL, p_c 0.3); this is the only place FAIL does not mean
  "contradicting evidence", and it is documented in compliance_detail.
- HARD doc-proof with no claim: the cell is `verification_only` (listed in verification_required, not scored, does not
  block), whatever the constraint's coverage-based role. *Coordinator's reading:* per-cell, not per-constraint.

## D052 — Reference ranking uses the final agreed truth state (2026-10-29, user decision)
Supersedes R3 of D037. The buyer's truth state evolves: `Turn.buyer_truth_state_after` shows the constraints with the
agreed relaxations applied (YES, BYPASS, and an accepted COUNTER at its value). `reference_ranking` is computed from the
FINAL agreed truth state; `reference_ranking_initial` (new `FinalOutcome` field) is the ranking before any concession.
Golden tests: the current ones stay (reveal disabled, max_bundle_size 2); one more golden run uses default settings
(progressive reveal on, max_bundle_size 4).

## Interpretations to confirm (coordinator, 2026-10-29)
1. Synthetic terms have `golden_param = None` rather than False (rule 1.1). 2. Coverage mixes structured evidence (whole
pool) with grounded normalised values (sampled candidates only). 3. SOFT constraints are always `negotiated`. 4. D046
merges only same-hardness members. 5. No-claim SOFT doc-proof = FAIL("no_claim"); no-claim HARD doc-proof = per-cell
verification_only. 6. Tightness: code tightens deterministically if the LLM's values leave the initial count >= min_shortlist.
7. Terms with a degenerate band are omitted. 8. Numbering: the user's unnumbered items became D051 (doc-proof claims) and D052 (reference ranking); D050 is the user's recommendation-targets addendum.

## D050 — Always recommend: BEST_AVAILABLE, match_original, per-tier targets, calibration (2026-10-29, user decision)
- **Targets** `[perfect-deal share, match %]`: lenient [0.90, 1.00], medium [0.80, 0.90], medium_strict [0.75, 0.85],
  strict [0.65, 0.75]; stored verbatim in `recommendation.tier_targets`. **Their exact meaning is NOT settled** (see the open
  question below); no calibration is implemented against them until it is.
- **Perfect deal** = the top-1 recommendation satisfies every negotiated constraint in the final agreed truth state.
- **match_original** = weighted share of the ORIGINAL negotiated constraints satisfied by the recommended candidate
  (HARD weight 2, SOFT weight 1, `verification_only` excluded; `recommendation.match_weights`). Stored per `RankingRow` and,
  for the top-1, as `GTConversation.top1_match_original`; `GTConversation.top1_perfect_deal` stores the boolean.
- **Always recommend something.** `NO_FEASIBLE_OUTCOME` is removed and `BEST_AVAILABLE` added; it is also used when patience runs
  out (`PATIENCE_EXHAUSTED` is removed; `best_available_reason` = no_feasible | patience_exhausted keeps the information).
  `FinalOutcome.best_available` holds the top-k (`recommendation.best_available_k`, 3) by `match_original`; each item has
  `unmet_constraints`, `verification_required` and `non_compliant`. A candidate with a negotiated HARD FAIL may appear only with
  `non_compliant = true` and it counts in the hard-violation metrics. `Action.BEST_AVAILABLE` is the negotiator's matching action
  (its `NegotiatorStep` must carry a ranking, like RECOMMEND).
- **Staged removal.** The contract now ADDS `BEST_AVAILABLE` and keeps the two old outcome values and `Action.NO_FEASIBLE_OUTCOME`
  marked deprecated, so the 488 existing tests keep passing. They are deleted in the dialogue step, once the negotiator and loop
  never produce them.
- **Calibration** (matching-engineer, deterministic, no LLM): for each D049 tightness candidate, simulate the full negotiation
  (truth beliefs, buyer policy, bundle search); keep the candidate whose outcome falls in the scenario's tier band. A scenario
  that cannot be calibrated gets qa_flag `uncalibrated` and is kept. Report the acceptance rate per tier.
- **Eval additions** (matching-engineer): per-tier perfect-deal share, match% distribution, share of conversations in band,
  BEST_AVAILABLE share. gt-auditor checks them against D050. The step-5 table gains per-tier calibrated targets vs simulated outcomes.

**Open question for the user.** "Targets [perfect-deal share, match %]: lenient [0.90, 1.00]" can be read two ways.
(A) Each tier has ONE band [lo, hi] that applies to both quantities: a scenario is calibrated when its top-1 `match_original` falls in
the band, and the tier's perfect-deal share across its conversations should also fall in the band. All four brackets are exactly
0.10 wide, which suggests bands. (B) Each bracket is a PAIR: the first number is the target perfect-deal share and the second the
target match%, with some tolerance to be agreed. The coordinator's default is (A). A boolean perfect deal cannot sit "in a band"
for a single scenario, which is why (A) puts the band on match% per scenario and on the perfect-deal share per tier.

## D044 amendment 3 — negotiated constraints are voiced before verification-only ones (2026-10-29, coordinator)
`select_opening` / `priority_order` sort by scenario role first (negotiated before verification_only), then the previous keys
(HARD first, key-spec types, category priority, original order). Without it, bids with many unscored HARD constraints (e.g. 27 in
the pilot bid) would spend the whole opening and both CLARIFY rounds on constraints that are not scored, and the scored ones that
drive the negotiation might never be voiced. Verification-only constraints are still voiced (user, D048), just later.

## D049 amendment — belief-side evaluation of the synthetic terms (2026-10-29, coordinator)
The negotiator hears the synthetic terms in the buyer's words and cannot see their provenance, so the dialogue layer maps a belief
to a catalogue field deterministically from its wording (budget/price/cost -> price x quantity; minimum order/MOQ -> moq in units;
rating -> rating; years in business/tenure/experience -> tenure years; state/location/region -> state) and evaluates it with the
same catalogue-field evidence the truth side uses. On the truth side `qty_vs_moq` is encoded as a slack fraction; on the belief
side the buyer's text states the cap in units, so the belief compares `moq` to that cap. Same truth, two encodings.

## D050 amendment — how the negotiator learns it must give its final offer (2026-10-29, coordinator)
Patience and the turn cap are hidden buyer parameters, so a negotiator under test can never know they ran out. When they do, the
buyer simulator emits one public, deterministic WRAP_UP turn ("I have to wrap up now. What is the best you can offer from what we
have discussed?", new `Action.WRAP_UP`) and the loop calls `step()` once more; the negotiator answers from the transcript with
RECOMMEND or BEST_AVAILABLE like any system under test. The GT negotiator recognises the phrase through one documented helper,
`is_wrap_up(utterance)`. The final `FinalOutcome.best_available` is evaluated on the TRUTH side (match_original, unmet_constraints,
non_compliant) for the candidates the negotiator itself chose, so hard-violation metrics reflect reality. The deprecated outcome
values and `Action.NO_FEASIBLE_OUTCOME` are deleted from the contract by the coordinator once no code produces them.

## Answers of 2026-10-29 (late) — user decisions
- **D050 tier targets: reading (A) confirmed.** One band per tier for BOTH perfect-deal share and match%: lenient .90-1.00, medium .80-.90,
  medium_strict .75-.85, strict .65-.75 (`recommendation.tier_targets`). Per scenario, calibration puts `top1_match_original` inside the
  band; per tier, the perfect-deal share across the tier's conversations should also fall inside it. Calibration is unblocked in meaning;
  its simulator must use the D053 front and buyer (D053 item 10), so it is built after D053.
- **D048 amended (Q3).** SOFT constraints with `evidence_coverage < scenario.negotiated_min_coverage` become `verification_only`, same cutoff
  as HARD. Synthetic commercial terms stay `negotiated`.
- **`scenario.verification_only_fail_blocks = true`.** Only MISSING evidence is unscored; an OBSERVED FAIL (real contradicting evidence) is a
  violation and still blocks/counts, for HARD and SOFT alike. Supersedes the literal "never scored" reading.
- **WRAP_UP accepted** (D050 amendment).
- **Fix now:** (1) the gain recorded for an accepted COUNTER must be measured at the counter value, not at the proposed one; (2) a BEST_AVAILABLE
  ranking is ordered by score like any ranking (NDCG/Kendall depend on it) and the offer (top-k by match_original) travels separately in the new
  `NegotiatorStep.offered_product_ids`. *Coordinator's reading of "ordered by score/match_original":* ranking by score, offer by match_original.
- **Live run Q1:** two stages, but only after D053, D054 and the CLI wiring.

## D053 — Relaxation spec (user decision; reference file now present at `docs/RELAXATION_LOGIC.md`, 2026-10-29; implementation delegated)
The user designated `docs/RELAXATION_LOGIC.md` (first cited as docs/reference/, the file lives one level up) (written for another codebase) as the binding spec for relaxation, superseding the
D016/D025/D045 search and refusal logic, the CLAUDE.md §3 buyer policy and the doc_to_pending part of D051 (recommendation/D050 unchanged).
**That file does not exist in this repository or anywhere on the machine** (checked 2026-10-29: repo, ~/Downloads, ~/Pythonstuff incl. recsysgt2),
and the section numbers the decision cites (§3, §5-§8.7, §11.3, §12, §16.1-16.2, worked example §8.6) cannot be read. The decision as stated by
the user, kept verbatim in substance so it is not lost:
1. settings.relaxation.gates: allow_hard_constraint_relaxation=false, allow_hard_product_specification_relaxation=false, immutable_kinds=["documentary"]
   (is_doc_proof is never relaxed, verification path only), soft_default_relaxable=true.
2. Coverage (spec 8.2, translated): decided by the relaxable rules that decide eligibility = SOFT negotiated rules (HARD only if the gate is on);
   verification_only rules never enter fronts.
3. Levels (§3/§5): [0, .02, .05, .10, .25, .50], default_max_deviation .25, Fraction arithmetic from the ORIGINAL value, no compounding; only numeric
   inequality rules relax, ==/bool/string get domain [0]; cost = sum weight * r_c / L_c.
4. Strict/hopeful need tables (§7); joint Pareto front (§8.3): exact enumeration of useful values, recovery counted in distinct SUPPLIERS, tie on products,
   then the lexicographic tuple; drop points changing more than max_bundle_size (4) rules; ERROR above max_relaxation_combinations (no silent fallback);
   menu = up to 5 authorized points with verified_after > V_s; reference choice = cheapest, record agrees_with_reference; diagnostic front (§8.7) at most
   once per scenario, the buyer can only ACKNOWLEDGE (add Action.ACKNOWLEDGE).
5. Bounds (§6): ACCEPT -> fixed, COUNTER -> max k, REJECT -> max 0; only tighten. Deviation (gap 16.1 #6): pin only the rules actually refused.
6. Buyer (§11.3), H = persona.tau (still LLM-picked inside the code-assigned band). Persona adds, LLM-chosen inside the ranges and consistent with the
   tier: max_relaxations [0,3], patience_rounds [1,4], decision_randomness [.1,.5], tolerance{constraint_id: 0..5}; seeded streams f"{seed}:personality"
   and f"{seed}:buyer"; alpha=6, beta=2, kappa=0 in settings. Deviations: per-item responses on multi-rule proposals (gap 16.1 #5); COUNTEROFFER only for
   medium_strict/strict, else REJECT; no H3 conditions.
7. Apply/validate (§12): originals immutable, Change records append-only (actor="buyer"), check_change 1-7, validate_transition; gt-auditor re-runs them on
   every GT turn.
8. Doc-proof: a supplier claim -> PENDING; the buyer's BYPASS acknowledges the verification list; no doc relaxation.
9. D049 terms: preferred_states cannot relax; the others are numeric inequalities.
10. The D050 calibration simulator uses this same front and buyer. Tests: port the §8.6 worked example and the §16.2 tests as unit tests.

## D054 — All prompting live and conversation-specific (user decision; sequenced AFTER D053)
1. `--live`: every LLM step of every scenario calls the model with that scenario's own inputs; synthetic fixtures, mock stubs and template fallbacks are
   forbidden in live mode (a missing/invalid response after retries raises an error and sets a qa_flag, never a stub). Mock mode is for tests only. The
   legacy pilot path is removed.
2. The cache is exact replay only: its key adds scenario_id, turn_idx and persona seed to the inputs, so no response is reused across conversations or
   turns; `--fresh` reads no cache; every call is logged live or cache_hit in provenance.
3. Per-conversation context, no generic text in any prompt (buyer: profile incl. formality/verbosity/regional voice, tier and traits as behaviour never
   numbers, the brief in the persona's words, transcript, this turn's decided action and exact values, revealed vs to-reveal; negotiator: transcript only
   plus this scenario's catalogue evidence for the candidates in the current menu as quoted snippets with evidence ids, the chosen menu point, allowed_numbers,
   a per-scenario sales persona generated once and stored; reframe/doc_classify/align/normalise/claim per bid).
4. Each reply must reference the previous turn's content.
5. gt-auditor adds: live-call share per run (100% on a --fresh run), exact-duplicate utterances across conversations (0), distinct-2 per speaker, banned
   template phrases, a sampled check that each reply addresses the previous turn.
6. Affected prompts are re-versioned (v+1); each header lists its per-conversation inputs.
*Why it waits for D053:* the negotiator prompts consume the D053 menu point and `allowed_numbers`, and the buyer decision they verbalise comes from the
D053 buyer, so building them first would be rebuilt.


## D053 amendment — translation of the spec's "coverage" to gtgen (coordinator, 2026-10-29)
The spec (§8.2, §16.2) lets only HARD relaxable rules decide coverage. gtgen's relaxable rules are SOFT negotiated constraints (HARD only when
`relaxation.gates.allow_hard_relaxation` is on; default off, CLAUDE.md §1.6). So in gtgen the rules that decide coverage in the joint front are the
relaxable SOFT negotiated rules; HARD rules are immutable. verification_only rules never enter a need table or front. Spec §16.2 tests port with
"HARD relaxable" read as "relaxable under the gates". Settings: `relaxation:` block. Persona v3 adds max_relaxations, patience_rounds,
decision_randomness, tolerance (all Optional on the contract).

## D055 — Proof requests (user decision, 2026-10-29)
The buyer may ask for proof of ANY attribute the catalogue claims, not only `is_doc_proof`. Per item, a seeded draw from `f"{seed}:buyer"` with
probability `proof_requests.rate_by_tier[tier]` (lenient low, strict high). Buyer action BYPASS; the cell status is unchanged (PASS stays PASS,
PENDING stays PENDING). The document is appended to that candidate's `pending_verification_docs` and the conversation list, with `turn_idx` and
attribute (`ProofRequest` on `Turn.proof_requests` and `FinalOutcome.proof_requests`). NO effect on p_feasible, utility, score, p_recommend,
match_original, perfect_deal or rank. The negotiator replies with `Action.ACKNOWLEDGE` and names the document using catalogue evidence only.
Test: ranking rows byte-equal except `pending_verification_docs` before/after. gt-auditor checks every BYPASS.

## D056 — Relaxation loop (user decision, 2026-10-29; makes D053 explicit)
Each negotiator turn: (a) re-derive state (active values, bounds, refusals, reveals, verification lists); (b) recompute need tables and joint front
from it; (c) propose a menu point or RECOMMEND / WRAP_UP / BEST_AVAILABLE. Buyer per item: YES = bound fixed + Change record; NO = max 0;
COUNTER = max k; BYPASS = verification list only. Truth loop from the buyer's decisions; belief loop re-derived from the transcript through
`negotiator_interpret`. Per turn a `RelaxationAnalysisRow` (front points, chosen point, bounds before/after, verified count before/after, truth and
belief). Ends: shortlist reached, patience out, empty front -> BEST_AVAILABLE. Golden tests: accept -> new front from fixed value; counter -> next
proposal <= counter level; reject -> rule never reappears; bypass -> front and ranking unchanged; >= 3 iterations in one conversation.
Metrics: `belief_bounds_accuracy`, `gain_estimate_error` (|negotiator_estimated_gain - truth_gain|). All gains use ONE shared role-aware
compliance function (verification_only excluded), the one used by ranking and calibration.

## Soft-only-blocked reporting (user, 2026-10-29)
The B-style table splits blockers into real_soft_blocked / synthetic_commercial_blocked / observed_fail_blocked per bid and tier, reported as measured.

## D053 rulings on matching-engineer questions (coordinator, 2026-10-29)
1. Levels: relative change = level x max_deviation; defaults levels [0,.5,1] x 0.30 = 0/15/30%.
2. V (confirmed_recovery) stays 0 in gtgen (no evidence rounds); the term is off.
3. Missing persona v3 fields: range midpoint.
4. The per-turn cap max_concessions_per_turn is superseded by persona.max_relaxations (D053 buyer replaces CLAUDE.md §3.5 buyer policy).
5. Persona.tolerance keys are constraint categories.

---
## D057 — Cleanup of commit 6eb11b5 (coordinator, 2026-10-31)
6eb11b5 (made by an unattended opencode session) deleted `tests/dialogue/test_golden_default_settings.py`, `test_golden_negotiation.py` and
`test_policy.py`, and rewrote `relaxation` in settings.yaml with spec key names the engine (`matching/rules.py`) did not read, so the engine
silently fell back to its defaults. (The file had no duplicate YAML keys; checked with a duplicate-key loader.) Ruling:
1. 6eb11b5 reverted with `git revert --no-commit`; the three test files restored from git (content of fec4412).
2. `relaxation` rewritten once, spec key names (RELAXATION_LOGIC.md §3): `gates.{allow_hard_constraint_relaxation, allow_hard_product_specification_relaxation,
   immutable_kinds, soft_default_relaxable}`, `level_mode: relative` (the spec's levels are absolute relative changes; the engine default "fraction_of_max"
   would have misread them), levels [0,.02,.05,.10,.25,.50], default_max_deviation .25, default_weight 1, max_relaxation_combinations 1000000.
3. The engine must read these names (matching-engineer); `allow_soft_relaxation` is dropped (not in the spec).
4. Dead key `negotiation.max_counter_rounds` removed (D053 replaced counter-counter rounds by buyer bounds). The restored golden/policy tests were adapted
   to D053 by the dialogue-engineer (old assertions encoded the superseded CLAUDE.md 3.5 policy); every adaptation is listed in that session's report.
   The product-spec HARD gate is stored but not enforced (no rule scope in the engine yet).

---
## D058 — Model: google/gemma-4-26b-a4b-it (paid) replaces the :free variant (user decision 2026-10-31; coordinator)
Checked against the OpenRouter API on 2026-10-31: the repo was ALREADY on the 26B (`google/gemma-4-26b-a4b-it:free`, D022); no live code used the 31B
(only `llm/client.py` default string and `tests/llm/test_client.py` fixtures name `gemma-4-31b-it`; these are cleaned up). The paid id
`google/gemma-4-26b-a4b-it` exists (262k ctx, $0.0675/M in, $0.225/M out at the cheapest listed endpoint) and supports response_format,
structured_outputs, seed. The `:free` variant is pinned to google-ai-studio (BYOK); google-ai-studio is NOT among the paid endpoints. Routing chosen:
`provider.order: ["google-vertex"]` (Google first-party, structured_outputs + response_format, max completion 235k), allow_fallbacks false,
require_parameters true. Reason: reference-quality hosting for a reproducibility-sensitive benchmark; cost is immaterial at this scale (stage 1 is
~30 calls x ~10k tokens). Cheaper endpoints exist (PrimeIntellect, NextBit; AkashML is fp4) and can be swapped in settings. The slug was NOT
verified by a live call. Cache: the cache key includes the model id, so every entry written under `...:free` (99 files in .cache/llm/) is a miss
under the new id; they are left in place, not deleted. The free-tier limits in settings (20 req/min pacing, ~10k tokens per call) were NOT changed.

## D059 — ItemResponse.constraint_ref is always the truth constraint_id (user request 2026-10-31)
Proposal items previously used belief ids (B3) and D055 proof-request items truth ids (S4-C3). Now ItemResponse.constraint_ref = truth id for both;
new `ItemResponse.belief_ref` carries the belief id. The Negotiator-side proposal (belief ids) is unchanged; the information barrier is unaffected
because ItemResponse is buyer-side GT.

---
## D060 — Personality diversity (user requirement 2026-10-31: every buyer personality must be different)
1. Stratification generalised from tau (D036) to the whole trait vector: tau, tolerance (per category), patience_rounds, decision_randomness,
   max_relaxations. For a run of N scenarios code draws, per trait, a seeded permutation of N equal strata over the configured range (Latin hypercube);
   each scenario gets one stratum per trait, the LLM picks the exact value inside it (validated), and the strata are stored in `Persona.strata`.
2. Company identity uniqueness pass: exact/near-duplicate company names (similarity >= persona.diversity.company_name_similarity) and (industry, city)
   repeats beyond max_same_industry_city_share are regenerated with a nonce bump (<= max_company_regenerations), never accepted silently; exhausted -> qa_flag.
3. Audit: `gtgen.eval.persona_diversity` (deterministic) gives pairwise trait distances (min, mean, histogram) and flags pairs under min_pair_distance;
   gt-auditor checklist item 11. Mock-mode check can only verify the stratifier (code), not LLM mode collapse; the LLM part is checked on live output.

## D061 — Realism audit (separate from correctness; user requirement 2026-10-31)
gt-auditor checklist item 12 plus deterministic `gtgen.eval.realism` (opening length/shape, progressive-reveal share, budget vs catalogue price range,
near-verbatim repeated turns, sales-persona distinguishability). Thresholds in settings `persona.realism`.
Also: negotiator cache seed derived from persona_seed (sha256 of f"{seed}:negotiator"), so the real persona seed never reaches the negotiator context.

---
## D062 — Correction: D058 reverted; ONLY google/gemma-4-26b-a4b-it:free via Google AI Studio (user, 2026-10-31)
The model is `google/gemma-4-26b-a4b-it:free` (https://openrouter.ai/google/gemma-4-26b-a4b-it:free), provider `google-ai-studio`, allow_fallbacks false.
Nothing paid; no google-vertex anywhere in config, prompts or code. D058's model/provider part is void (its D059 sibling stands). The 99 `.cache/llm`
entries under the `:free` id are valid again (never re-key or discard). The fixed fixture identity (client FIXTURE_MODEL) already equals the `:free` id, so
mock fixtures need no change. Real endpoint limits: 16,000 tokens per call TOTAL (input + output) and 20 calls per minute. Settings: `llm.token_limit_total`
16000, `llm.max_tokens_per_call` 15000 (budget with 1000 headroom), rate_limit 20 rpm / 3.2 s. Required work: per-call token estimation with
bounded completion, dynamic batch sizing by estimated tokens (reframe/doc_classify/align/doc_claim) with batch size and estimated tokens logged per request in
provenance, a decision on structured output from one live smoke call (cap 5 requests), and a from-scratch stage-1 estimate from real token counts.

---
## D063 — Per-role API keys (user, 2026-10-31)
Several OpenRouter accounts share the per-account limits. `llm.api_key_roles` (prep, claims, persona, buyer, negotiator) -> env vars in `.env`
(template `.env.example`); a missing/empty/`REPLACE_ME` role key falls back to `OPENROUTER_API_KEY`. Pacer per key; keys never in cache keys, logs or provenance.
HTTP retry (user): 6 retries, linear 8,16,24,32,40,48 s (settings `llm.http_retry`). The 50 requests/day limit is handled by the user outside the code (no ledger).

## D064 — Token logic (user, 2026-10-31; supersedes the 16,000-token assumption of D062)
The real per-request ceiling is the 262,144-token context (input + output combined), output <= 32,768. Settings: token_limit_total 262144,
max_tokens_per_call 240000, max_completion_tokens 32768, new batch_completion_target 12000 (planning target for one call's output), batch_max_constraints 80,
doc_claims.batch_size 60, timeout_s 600. Rationale: batching is the best defence against 50 requests/day; caps keep batches reliable (quality, latency,
validation retries). Consequences accepted: batch composition changes -> cache keys of batched prep calls change (the 99 old entries remain but prep calls
re-run under --fresh anyway); recorded mock fixtures need pinned legacy batch settings in tests.

---
## D065 — Conversation naturalness pass (user, 2026-10-31; reference: optimal-conversation example, turn flow only)
Turn flow of the example already matches D053/D055/D056; no contract or loop change. Surface style is NOT copied: buyers never speak tags
([HARD], [SOFT - ±8%], [NEW HARD]); no 5-answer enumerated burst (reveal over 2-4 short turns, `reveal_max_per_answer` 4 -> 2); the negotiator never speaks
internal arithmetic (candidate counts, joint gain, cost) - plain business language only, numbers stay in relaxation_analysis/ranking; buyer register follows
tier; sales persona name/tone varies per scenario. Work: prompts v3 with negative few-shot examples (llm-engineer), deterministic realism checks
`no_inline_tags`, `no_spoken_arithmetic`, `enumerated_burst` (matching-engineer), auditor items 12/13 extended (verified on live output only).

## D066 — No hard request cap; retry/skip policy; deterministic-first prep (user, 2026-10-31)
1. No request cap: `--max-requests`/`--approved-calls` no longer stop anything; HTTPGate only counts and logs progress. The first-live-call failure guard stays.
2. Per call up to 6 retries (schema validation `llm.max_retries` 6; HTTP 6 linear retries 8..48 s, paced at 20 rpm). Exhausted => `ScenarioSkipped`
   (bid, persona seed, prompt, last error): logged, written to skipped.jsonl and the run summary; the run continues with the next scenario/bid.
3. `structured_output` stays `auto` (smoke call: fenced JSON recovered by the stripper, 0 retries).
4. Deterministic first, LLM fallback for doc_classify (keyword/regex table), align (token/synonym name matcher; value comparison was already deterministic in
   matching) and doc_claim (standard id + conformity word -> quote built from the listing and verified by verify_quote). Every item logs path=rule|llm.
   reframe and persona stay LLM. Ambiguous => LLM, never guess. The planner predicts rule-resolved items offline.
Amendment D065: clarify_max_rounds 2 -> 3 (cap 2 per answer => up to 6 revealed over 3 short turns).

## D067 — Adopt the user's proven retry logic (user, 2026-11-01; 300+ calls/day achieved with it)
First stage-1 live run: 8 ok / 16x 429 ("exceeded your current quota", Google AI Studio via OpenRouter metadata.raw) / 1x 502 / 1 dropped connection; both
scenarios skipped at doc_claim. Our retry ignored the provider's body hints and fixed the wait at 8..48 s. Adopted: parse Retry-After header AND the JSON
body (nested OpenRouter `error.metadata.raw`, `details[].retryDelay`, message 'retry in Ns'); wait = max(linear 8*attempt, hint + 5 s), capped at 300 s;
400/401/402/403/404/422 fail immediately; disconnects/timeouts/5xx/429 retried; empty-content responses get a diagnostic (finish_reason, usage, model); reasoning
disabled (`llm.disable_reasoning`). Retry count stays 6 (D066). max_retry_after_s 90 -> 300 (the old fail-fast treated long hints as a daily quota).

## D068 — Real limits: 16K tokens/minute per key is the binding constraint (user, 2026-11-01); supersedes the batching direction of D064
Google AI Studio free limits for Gemma 4 26B, per API key (each OpenRouter account has its own Google key): 30 requests/min, 16K tokens/min, 14.4K requests/day;
context 262,144, output up to 128K. The live runs 429'd with "exceeded your current quota" because 20-30K-token batched calls exceed 16K tokens/min. The
50/day limit no longer applies (14.4K rpd). Change: small batches (max_tokens_per_call 10,000 est. in+out, batch_completion_target 3,000, batch_max_constraints 25,
doc_claims.batch_size 12, max_completion_tokens 8,000), rpm 30, and a per-key rolling-minute TOKEN pacer (tpm_limit 16000 x tpm_safety 0.8, estimated input + planned
output) that sleeps before a call that would exceed the window. Keys are per role so heavy roles do not starve others. Also: tolerate trailing commas in fenced JSON
(first reframe reply failed json_invalid on a trailing comma, wasting a call). Cache keys of batched prep calls change again (batch composition).
Amendment D068: max_tokens_per_call 10000 -> 14000 and tpm_safety 0.8 -> 0.9 (dialogue negotiator prompts are 10.0-10.5K est and would raise LLMBudgetError; one such call still fits 16K/min; batches stay small via item caps).

## D069 — First successful live pilot (71 requests, all 200) exposed three defects (2026-11-01)
Run pilot_20261031_201309_b2ee3452 (488257 + 341930): both conversations ended after 2 turns (OPEN_REQUEST, BEST_AVAILABLE). Root causes (dialogue-engineer replay from cache):
1. Scenario unsatisfiable by construction: negotiated HARD constraints with 0 PASS cells (488257 water_resistance; 341930 material_of_body, bonnet_material_standard,
   coating); `evidence_coverage` counts observed FAIL, so such constraints still became `negotiated`; calibration only tightens commercial terms. [PENDING user decision: D048 role rule]
2. CLARIFY skipped on the infeasible path (negotiator.py:748-767): fixed - CLARIFY runs before an empty-menu BEST_AVAILABLE and before the first PROPOSE.
3. Final turns (RECOMMEND, BEST_AVAILABLE) were fixed template strings (_best.py, _recommend_text): violates D054. Ruling: wording is LLM-generated (`negotiator_final`), FACTS stay
   deterministic and code-built from catalogue/beliefs; code validates numbers/names against the facts (rule 8 preserved). Amends D050's "deterministic wording" only.
Also: negotiator_extract v2 (budget ceiling '<=', canonical supplier_rating/trading_years names).

### D069 amendment — zero-PASS demotion (user decision, option 3, 2026-11-01)
D048 rule 6 stays as is for any constraint with >= 1 PASS, however few (narrow is a legitimate negotiation). New sub-rule: a HARD or SOFT constraint (never a synthetic
commercial term) with exactly 0 PASS across the FULL candidate set becomes `verification_only` (dead on arrival: unsatisfiable by construction, distinct from thin evidence).
The buyer still voices it and it is listed in `verification_required`. Two demotion paths, told apart by `BuyerConstraint.role_reason`: `low_coverage` (D048, evidence coverage
< negotiated_min_coverage) and `zero_pass` (this rule). Evaluated on the TRUTH-side compliance cells before calibration; recorded in `scenario_meta` (counts per reason).
Setting `scenario.demote_zero_pass` (default true). Contract: new optional `BuyerConstraint.role_reason`. CLAUDE.md rule 6 amended accordingly.

## D070 — Supplier rating and years are ranking-only; supplier priority location > years > rating (user, 2026-11-01)
Stating a minimum rating or years of experience in the conversation removes the purpose of a recommendation, so neither is ever spoken (buyer or negotiator) nor a constraint:
`min_supplier_rating` and `min_tenure_years` leave `scenario.commercial_terms` (remaining synthetic terms: budget, quantity-vs-MOQ, preferred states). They stay as
RANKING FEATURES (`rating_bayes`, `tenure_years`): the recommendation orders candidates by the best rating and longest tenure available. Priority among supplier features:
location (voiced as the preferred-states term) first, then years of experience, then rating; years rank above rating to handle the cold-start problem (new suppliers have
few ratings). Enforced in code: `recommendation.supplier_priority`, default weights location .15 > tenure .12 > rating .08, persona weights forced into the same order.
Consequences accepted: fewer relaxable terms (rating/tenure were two of the SOFT relaxation levers); match_original no longer includes them. Realism check `no_supplier_stats`.

D070 amendment (user, 2026-11-01): strict-tier personas get `max_relaxations` in [1, 2] (`persona_ranges.max_relaxations_by_tier.strict`), other tiers [0, 3]. Applied after tier binning (clamp), recorded as `max_relaxations_clamped` in the persona extra.
D070 note: recommendation.tenure_cap_years 10 -> 20 (matching-engineer found 53.5% of suppliers at/above 10, flat feature; p90 16, max 24).

## D072 — Clarify budget, relaxation-first policy, closing turn, richer opening (user, 2026-11-01)
1. Clarify is capped at 1 round (2 only for a complex brief: opening states >= 9 constraints) and asks ONLY about constraints already stated in the opening (hard/soft status, unclear values).
   It never draws new attributes out of the buyer (this also stops the buyer inventing 'top-grain', 'OEM authorization'; audit defect 5). Settings: clarify_max_rounds 1, clarify_max_rounds_complex 2.
2. After clarify, the relaxation loop (D053-D056: single constraints and bundles up to 3-4, most newly-eligible candidates first) runs on the buyer's own stated constraints. With 0 feasible candidates the
   loop is MANDATORY before any BEST_AVAILABLE; the conversation reaches a final outcome only after a relaxation pass was attempted on every stated constraint (within max_relaxations / patience_rounds);
   HARD constraints are still never relaxed (the pass records them as checked/refused).
3. The final turn (RECOMMEND or BEST_AVAILABLE) closes the conversation: no trailing question, no 'which would you like me to look into', no question mark.
4. Richer opening: opening_min/max 7-9 (was 5-7), opening_max_sentences 6, so more concrete material is voiced upfront.
5. Scenario validity (separate): 231682's brief carried two contradictory mass values (685 gms and 2.3 kg); data-steward finds the upstream cause; zero-PASS must scan every HARD constraint.

## D071 — doc_claim volume (user, 2026-11-01)
Shared claim cache/dedup keyed by (normalised required standard, listing_text_hash) - never by constraint ids, so table stage and dialogue (belief ids) share it; a changed listing is a miss. Dedup of identical (listing, standard) pairs within a bid with broadcast of one classification; widened deterministic rule for standard-id + conformity-word claims (negations/hedges/paraphrases stay on the LLM); doc_claims.batch_size is a CAP (12 -> 24) under the dynamic token budget. Note: tokens/minute binds, so bigger batches cut requests not tokens; the savings come from fewer items sent. Auditor checklist item 14 verifies stale cache, dedup broadcast and rule false positives.

D072 contract change: `RelaxationAnalysisRow.constraint_attempts: dict` (per-constraint relaxation-attempt ledger; GT-visible so the auditor can verify 'every stated constraint was attempted before BEST_AVAILABLE').
Open (user): D053 item 9 treats categorical SOFT as non-relaxable, so `preferred_states` can never be relaxed; in 488257 this ends the negotiation at turn 1 even with a feasible budget+states bundle in truth.

## D074 — Scenario validity gate (D072 point 5; 2026-11-01)
A scenario is negotiated only if it has >=1 negotiated non-synthetic product constraint, loosest_feasible >= min_shortlist, initial_feasible < min_shortlist and a SOFT relaxation path (scenario.require_valid_for_negotiation). Invalid ones are skipped with reason. Tightness fix: snap below the band edge (budget floor = realism floor) so the initial feasible count reaches the target. Bid scan (docs/coverage_llm.md): only 6 bids have products; valid: 488257 (marginal), 209245.

## D075 — nothing_verified (2026-11-01)
RECOMMENDED requires >= min_shortlist candidates that are feasible AND verified (at least one scored PASS/PENDING on a negotiated specification belief; budget/MOQ/location do not count). Otherwise BEST_AVAILABLE with best_available_reason 'nothing_verified' (contract Literal extended). Found in live run r6 (209245 recommended with every spec unverified and 0 truth-feasible candidates).

## D076 — preferred_states becomes relaxable (user decision, 2026-11-01; amends D053 item 9)
Blocked three audited runs (488257 x2, 341930). Categorical set-widening with LEVELS, not all-or-nothing: level 1 = add up to 2 adjacent/common supplier states, level 2 = drop the
state preference. It competes in the normal joint bundle search (joint gain vs cost, D053) - never auto-applied; a budget-only or delivery-only bundle with more gain at lower cost still wins.
Buyer answers by tier with the same alpha/beta/kappa sigmoid and YES/COUNTER/NO; strict personas resist location harder (`relaxation.location.strict_resistance`). Own persona category LOCATION
(tolerance/counter band) so a lenient budget tolerance never makes location free. Settings: `relaxation.location.*`; `scenario.commercial_terms.category.preferred_states: LOCATION`.
Separate bug (user): the calibrator claimed a path for all 11 candidates while every simulation ended no_options with 0 rounds; after this lands the bid suitability scan is re-run against
what the real search finds (not the calibrator), and the calibrator/validity gate are reconciled with the dialogue's relaxation rules. If still too few bids reach a deal: widen the level grid for very large gaps (e.g. budget beyond +50%) or widen the bid sample.

## D081 — Calibrator/validity-gate reconciliation with D075 nothing_verified (root cause of the 209245 2-turn live run; 2026-11-01)
Root cause of a live pilot (`pilot --live --bid 209245`, run `pilot_20261101_135245_62d8f15b`) ending after 2 turns
(`iterations: 1`, `proof_requests: 0`, `best_available_reason: "nothing_verified"`) even though the buyer persona's
`patience_rounds`/`patience_turns` nominally allowed more rounds: this was NOT the D079 fallback (`_call_or_fallback`)
misfiring and NOT a transient LLM/network issue (the live log's 502 retries all succeeded) — the negotiator's D075 gate
(`dialogue._verified.verified_feasible`) is working as designed, but D050's deterministic calibrator
(`matching.simulate_negotiation`, used by `scenario_build._calibrate` / `data.calibrate.top1_match_original`) never
applied that same gate. It only checked the plain, role-aware feasible count (`feasible_product_ids` /
`feasible_set`, which already treats `VERIFICATION_ONLY` cells as unscored/non-blocking per D048), so it could — and
for 209245 did — predict a multi-round negotiation (`chosen_step: 4`, `scenario_meta.calibration.n_rounds: 2`) for a
scenario whose real outcome the live negotiator always reaches in 0 rounds: bid 209245's HARD constraints (BIS
certificate number, type test report, ...) have ZERO candidate-pool evidence anywhere (`role_reason: zero_pass`), so
every spec belief is `VERIFICATION_ONLY`, nothing ever counts as "verified" (D075), and BEST_AVAILABLE fires on
the negotiator's very first turn — before any CLARIFY, PROPOSE_RELAXATION or (D055) proof request can happen, since
none of those paths exist for a constraint that was never `NEGOTIATED` to begin with. This exact mismatch was already
flagged as an open "separate bug" under D076 ("the calibrator claimed a path ... while every simulation ended
no_options") and is now closed:

1. New shared gate `gtgen.matching.compliance.verified_feasible_ids(feasible, constraints, cells, roles)` (+ its
   `is_spec_constraint` / `_commercial_kind` helpers), generalising the belief-only D075 gate via `ref_of`/`hardness_of`
   (`ConstraintT` = truth `BuyerConstraint` or belief `BelievedConstraint`) so truth-side code can use it too.
   `dialogue._verified.verified_feasible` / `is_spec_belief` now delegate to it (same name/signature, no caller change).
2. `matching.simulate_negotiation`: the `len(now) >= min_shortlist` check now also computes `verified_feasible_ids`
   on the current round; if verified count is still below `min_shortlist` it sets `stop_reason = "nothing_verified"`
   (new value, breaks immediately, 0 extra rounds — relaxing a SOFT term can never manufacture catalogue evidence for
   an unrelated HARD spec constraint) instead of the misleading `"shortlist"`.
3. `data.calibrate.top1_match_original`: `n_feas` (the RECOMMENDED-vs-BEST_AVAILABLE split used to pick the
   calibration target and `top1_match_original`) is now `len(verified_feasible_ids(...))`, not the plain
   `feasible_product_ids(...)` count — so calibration meta (`chosen_step`, `n_rounds`, `chosen_top1_match_original`)
   is consistent with what the live negotiator will actually produce.
Tests: `tests/matching/test_simulate_nothing_verified.py` (new) pins `verified_feasible_ids` on truth constraints,
`simulate_negotiation`'s new `stop_reason`, and `top1_match_original`'s corrected outcome. `n_neg_product` / `loosest_feasible`
(D074 validity gate) are left as they were: they answer "can a shortlist be reached at all", a materially different
question from "can it be RECOMMENDED", and bid 209245 is one of only 2 bids in the whole scanned pool with any
products at all (D074) — BEST_AVAILABLE is still the correct, by-design terminal outcome for it (D050); this decision
makes the pipeline stop mis-predicting a longer negotiation than the evidence supports, not force one into existing.

## D082 — tau_weight sigmoid amplification + all-tier counter (2026-11-04, deliberate persona-realism tuning)

**tau_weight change (Change 1):** Added `tau_weight: float = 1.5` to `BuyerParams` / `RelaxConfig` / `settings.yaml`, used in `accept_probability` as `softness = tau_weight*(1 - tau)`, replacing the previous `softness = (1 - tau)` embedded in `z = alpha_eff*(1-H-d)`. Default `1.5` was chosen so at mid-range (`tau=0.4`, `d=0.5`, `alpha_eff≈4.2`) the sigmoid lands near `p≈0.84`, giving the requested +20% vs baseline. Tuning guide: `1.2` → gentler (`~0.70`), `1.8` → stronger (`~0.93`). Not a bug fix — a persona-realism tuning.

**Counter logic change (Change 2):** Removed the tier gate (`_COUNTER_TIERS`) and the `tol >= 1` floor from `buyer_decide`. Counter rule is now `current_level <= tol < r` for any tier (`lenient` through `strict`), making countering easier and avoiding flat refusal when a buyer's tolerance floors to `0`. Also updated `CLAUDE.md` §1.5/§3, module docstring, and all affected tests (updated assertions, not deleted).

## D083 — Retry prompt bounded + budget errors skip the bid (live all-bids crash; 2026-11-04)

**Symptom:** the live all-bids pilot aborted on `GEM/2019/B/242710` with
`LLMBudgetError: Prompt too large: ~13659 est. prompt tokens leaves 341 completion tokens`. The bid's 38 constraints
planned into two small reframe batches (22 items / ~2483 actual prompt tokens; 16 items / ~2161), so the first attempt
was never the problem.

**Root cause:** `LLMRunners._call` accumulated every rejected answer: each retry appended the full assistant JSON
(`parsed.model_dump_json()`) plus the correction to the running `messages` list. With `llm.max_retries: 6` a batch that
kept failing its output check grew the request on every attempt; the seventh attempt exceeded `max_tokens_per_call`
before any HTTP request was sent, and `LLMBudgetError` propagated out of `prepare_bid`, killing the whole run.

**Fix:**
1. Retries are now `base_prompt + [most recent rejected answer, correction]`, not the whole history. If even that pair
   would not leave `min_completion_tokens` of room (checked by `LLMRunners._fits_budget`, which mirrors
   `LLMClient._build_budget`), the rejected answer is dropped and only the correction is appended.
2. `_call` converts `LLMBudgetError` to `ScenarioSkipped` (D066): an oversized/hopeless batch skips that bid, and
   `cmd_pilot` already continues with the next bid. Auth failures (401/402/403) still propagate.
3. The client's own schema-validation retries (`_validate_loop`) got the same treatment (latest rejected reply only,
   dropped when it would not fit), so that path cannot grow into the same budget error.
4. A retry is a **new sample**, not a cache replay: `_call` adds `_retry_attempt` to `extra_inputs` for attempts > 0,
   so the cache key differs from the rejected attempt. Attempt 0 keeps the original key (existing fixtures / warm
   caches still work). Without this, a warm cache replayed the same bad answer for all 6 retries.
5. `reframe_value_problem` ignores whitespace around punctuation when matching `allowed_values` / numeric values:
   the live model wrote `"Bronze grade LTB 2, 3, 4 or 5 of IS 318"` for the allowed value
   `"Bronze grade LTB 2,3,4 or 5 of IS 318"`, which was semantically correct but rejected 6 times.
6. `CLAUDE.md` §LLM client rules updated to match the real retry policy.

Tests: `tests/llm/test_prompts_runners.py::test_retry_does_not_accumulate_rejected_answers`,
`::test_retry_drops_the_rejected_answer_when_it_would_not_fit`, `::test_budget_error_skips_the_bid_instead_of_aborting`;
`tests/llm/test_client.py::test_retry_attempt_bypasses_the_cached_rejection`,
`::test_validation_retry_then_fail` (bounded-context assertion);
`tests/llm/test_reframe_values.py::test_allowed_value_ignores_whitespace_around_punctuation`.
Simulation on the failing bid holds the reframe prompt at ~3568 tokens across all 7 attempts (was ~13659 on the last);
the live re-run now uses fresh live calls for every retry instead of replaying the cached rejection.

## D084 — Pooled API keys for `doc_claim` + one-bid-at-a-time live runner (2026-11-04)

**Symptom:** one live bid spent ~15-20 minutes in `doc_claims`: ~19 batches of 24 items, each waiting ~50 s in the
token pacer because the `claims` account is capped at `tpm_limit: 16000`, while the other four accounts were idle
(the stages are sequential). The all-bids loop also ran every bid inside one process, so a bad bid could stall the
whole run and all bids shared one limiter history.

**Decision (user, 2026-11-04):** pool all five accounts for the claims stage, and run exactly one bid per process.

1. New optional setting `llm.api_key_pools.<role>: [ENV_VAR, ...]` (currently `claims` = all five role variables).
   `LLMClient._resolve_api_key` round-robins over the **usable** pool keys for the current prompt role; if none is
   usable it falls back to `llm.api_key_roles.<role>` and then `llm.api_key_env`, so a missing pool key can never
   make the client unusable. The cursor is per role and lock-protected.
2. The limiter is keyed by env-var name (D063), so each pooled account keeps its own `RateLimiter` / token pacer;
   the pool increases throughput instead of just moving the same wait around. Pool keys are included in
   `_all_key_values`, so they are redacted from logs/errors exactly like role keys. The cache key is unchanged
   (D018): the key used to obtain an answer never affects `llm_call_id`.
3. `scripts/live_all_bids.sh` now runs one `python -m gtgen.cli pilot --live --personas 1 --bid <bid>` subprocess at
   a time from `data/raw/final_constraints.csv` (the six runnable bids; set `METADATA=data/raw/gem_bids_metadata.csv`
   to scan all metadata bids), writes one log per bid under `out/logs/`, records failures in
   `out/logs/failures.txt`, and continues with the next bid. A positional bid argument runs exactly one bid.
   `--personas` stays 1 (D080); the category's tau sub-bands are still chosen inside `build_scenario` by
   `personas_for_category`.

Tests: `tests/llm/test_api_keys.py::test_key_pool_round_robins_only_usable_keys`,
`::test_key_pool_falls_back_to_role_then_default`, `::test_pool_keys_are_redacted`,
`::test_pooled_calls_rotate_keys_and_use_separate_limiters`,
`::test_real_settings_claims_pool_uses_every_role_variable`.

**Live validation (2026-11-04, `GEM/2020/B/626099`, `--fresh`):** `doc_claims` needed 6 LLM chunks (133 items after
dedup/prefilter) and finished in ~60 s; the provenance shows the exact rotation
`CLAIMS -> PREP -> PERSONA -> BUYER -> NEGOTIATOR -> CLAIMS`, and the token pacer logged a single 7.2 s wait (the
first `claims` key) instead of ~50 s per chunk. The old single-key path would have taken ~5 minutes for the same six
chunks.

## D085 — Accept a leading JSON object when the model appends reasoning (live all-bids stall; 2026-11-04)

**Symptom:** the live `GEM/2019/B/209245` run spent >15 minutes in the negotiation loop. `negotiator_final.v4` returned
a valid JSON object followed by prose/code-fence reasoning (`{"utterance": ...}\n```\nWait, ...`), so the client's
`strip_code_fence` + `model_validate_json` rejected every attempt with `json_invalid: trailing characters`. Each retry
was re-sampled live and waited 30-55 s in the token pacer, so the bounded 7-attempt retry loop still took minutes.

**Fix:** `LLMClient` now extracts the first complete JSON object when the reply **starts** with one
(`first_json_object` / `json.JSONDecoder.raw_decode`), ignoring trailing prose. Prose *before* the JSON is still
rejected in structured-output mode (the D024 behaviour is pinned by `test_prose_with_embedded_json_fails_and_retries`);
`prompt_json` keeps its existing first-`{`..last-`}` extractor. The structured-output provenance records the accepted
first attempt as `first_attempt_valid: true` and `fallback: "trailing_text"` so the model's non-compliance stays
visible. The accepted text is still schema-validated, and `raw_content` in the cache is unchanged.

Tests: `tests/llm/test_client.py::test_first_json_object_ignores_trailing_prose`,
`::test_trailing_prose_after_leading_json_is_accepted`; the existing fence/prose tests still pass.

## D086 — Pool dialogue keys too; checkpoint every finished conversation (2026-11-04)

**Symptom:** the live `GEM/2019/B/209245` run took **12 m 06 s** for one bid (21 live calls). The claims pool was
never exercised because all `doc_claim` results were cache hits; the wall time was almost entirely the **single
`buyer` and `negotiator` accounts** waiting 30-55 s in the token pacer between dialogue calls (plus one 502 retry).
The pilot also accumulated conversations in memory and wrote `conversations.jsonl` only after the whole loop, so a
timeout/crash lost every completed conversation.

**Decision (user, 2026-11-04):** pool the dialogue keys as well, and checkpoint each conversation as it finishes.

1. `llm.api_key_pools` now also maps `buyer` and `negotiator` to all five role variables (the `claims` pool is
   unchanged). The round-robin and per-key limiter code is the same as D084; the logical `api_key_role` stays
   `buyer`/`negotiator`, while `api_key_env` in provenance records the actual account used. This supersedes the
   “dialogue still uses role keys” clause of D084. `prep` and `persona` keep their role keys (few calls).
2. `cmd_pilot` creates `out/gt/<run_id>/` before any LLM work and writes `run_meta.json` (run id, bids, mode, fresh,
   personas, start time). Each `assemble_conversation` result is appended to `conversations.jsonl` with
   `flush()` + `fsync()` immediately (`append_conversation`); skipped rows are appended to `skipped.jsonl` in the same
   way. The final loop no longer rewrites either file. A killed/timed-out process therefore keeps every completed
   bid/persona. `scripts/live_all_bids.sh` already runs one bid per process, so each bid is also a process-level
   checkpoint.
3. `write_conversations` remains the bulk/rewrite API for tests and explicit re-writes.

Tests: `tests/llm/test_api_keys.py::test_real_settings_dialogue_pools_use_every_role_variable`;
`tests/dialogue/test_negotiator_loop.py::test_government_term_flagged_and_jsonl` now also asserts that
`append_conversation` adds a second line without rewriting the first.

**Live validation (2026-11-04, `GEM/2019/B/209245`, warm cache):** completed in 12 m 06 s with outcome `RECOMMENDED`,
`top1_match=0.75`, `perfect=True`, 6 turns. D085 accepted the leading JSON plus trailing prose on the final calls;
the final turn still fell back to the template after the content check (`preferred states` internal-name guard), so
the QA flags include `template_fallback:negotiator_final`. The run wrote its conversation at the end (pre-D086); the
next live run will use the pooled dialogue keys and write incrementally. The fallback itself was also fixed:
`humanise_label` now maps the spaced label `preferred states` to `supplier location`, so the deterministic closing
text no longer trips the same internal-name guard (test in `tests/llm/test_d072.py`).

## D087 — Short retry budget for the closing turn (2026-11-04)

**Symptom:** the fresh live `GEM/2019/B/209245` run still took **12 m 02 s** even after D086 pooled the dialogue
keys. The token-pacer waits fell to ~76 s total, but `negotiator_final` made **7 live calls** (the initial attempt
plus 6 retries) because its content check kept rejecting the internal name `preferred states`. Each live attempt
could take minutes on the free provider, so the closing turn dominated the run.

**Decision (user, 2026-11-04):** the closing turn already has a deterministic, safe fallback (D078), so cap its
live retries at `llm.max_retries_final: 2` instead of the global `max_retries: 6`. Other prompts keep the global
budget. `_call` / `_call_or_fallback` accept an optional `max_retries` override; only `negotiator_final` passes
`final_max_retries`. The fallback is still tagged `template_fallback:negotiator_final` and remains visible in QA.

The label fix from D086 (`preferred states` -> `supplier location`) also removes the specific content-check
failure that triggered the retry chain. The two changes are complementary: the label fix should make the first
attempt pass; the shorter budget bounds the worst case.

Tests: `tests/llm/test_d069.py::test_final_exhaustion_falls_back_to_a_deterministic_recommendation` now uses the
final budget and asserts the exact call count; `::test_final_retry_budget_is_shorter_than_global` pins the setting.

## D088 — Align short cable names and normalise verbose extractor values (2026-11-04)

**Symptom:** the fresh `GEM/2019/B/209245` run ended `BEST_AVAILABLE` / `nothing_verified` after two turns. The
extractor emitted `cable_size`, `insulation`, `sheath`, `number_of_cores="3"` and a compound
`conductor_material="Copper (EC grade) stranded conductor"`. `cable_size` tied with `cable_type` on plain token
overlap and lost the alphabetical tie-break, so B3 checked the wrong catalogue field; `"3"` vs catalogue `"3 Core"`
failed the count-unit conversion; and the verbose text values never matched the catalogue's `"HR"`, `"ST2"` or
`"Copper (EC grade)"`. Every negotiable spec cell stayed UNKNOWN, the belief roles were demoted to verification-only,
and the verified-feasible gate refused to recommend.

**Fix (deterministic, no prompt change):**

1. `dialogue._util` canonicalises common cable aliases before the exact-name test: `cable_size` -> `size_sqmm`,
   `insulation` -> `insulation_type`, `sheath` -> `sheath_type`, `submersible_cable_quantity` / `cable_quantity` /
   `order_quantity` -> `total_quantity`, `supplier_state` -> `preferred_states`, and `core` / `cores` ->
   `number_of_cores`. The value-based tie-break in `pair_score` is unchanged; the alias makes the correct field an
   exact match (3.0) instead of a 0.5 tie.
2. `matching.units` adds `core` / `cores` to the count dimension, so `number_of_cores=3` (unit `cores`) compares
   numerically with catalogue `"3 Core"` instead of returning UNKNOWN for an unconvertible unit.
3. `Negotiator._normalise_aligned_values` replaces a verbose `=` value with the aligned catalogue's own value only
   when that value appears as a word-boundary substring (either direction) and is specific (not a bare number,
   generic boolean or `as per ...` text). `conductor_material` becomes `"Copper (EC grade)"`, `insulation` becomes
   `"HR"`, `sheath` becomes `"ST2"`; list/`in` values and numeric beliefs keep their original form. `"HR"` is a
   legitimate insulation type even though it is also the hour unit, so the unit-only filter is deliberately not used
   here.

**Result in the synthetic 209245-shape test:** `cable_size` aligns to `size_sqmm`, B2/B3 pass across the pool, the
belief roles stay negotiated, and `verified_feasible > 0` (the step is no longer `BEST_AVAILABLE`/`nothing_verified`).

Tests: `tests/dialogue/test_d088.py` (alias scoring, `cable_size` alignment, core-unit conversion, `3 Core`
compliance, and the 209245-shape integration).

**Live validation (2026-11-04, `pilot_20261104_013353_624c4526`):** the single-bid `GEM/2019/B/209245` run
completed cleanly with one persona (`seed=1`, `tau=0.482`, band `[0.42, 0.5]`, tier `medium`, patience 4):
7 turns, `BEST_AVAILABLE`, `best_available_reason="patience_exhausted"`, 28 live calls and 0 cache hits. The
conversation was appended/fsynced before process exit. The run also surfaced two further deterministic gaps:

1. The extractor split the buyer's cable-size range into `cable_size_min` / `cable_size_max`; the old one-belief-per-
   alignment-key rule left one of them unscored. Range variants of one canonical attribute now share the same
   catalogue alignment, and both names canonicalise to `size_sqmm`.
2. Catalogue `size` is `"Sq mm"` while the extractor emits `"Sqmm"`; `matching.units` now has an area dimension so
   the two spellings convert instead of returning UNKNOWN.

With those fixes the reference top (`2853286500333`) is no longer blocked on its size cells. It still is not the
negotiator's top because its catalogue row has no `conductor_material` evidence, so the belief-side hard cell stays
UNKNOWN; that is a conservative, data-side outcome. The negotiator top (`2852898502533`) passes conductor material
and cores and fails only the soft budget term (`350000 > 200000`), so it appears in `best_available` with
`unmet_constraints=["budget_inr"]`. The truth-side `match_original=1.0` for the reference top counts only the
negotiated truth constraint (`number_of_cores`), not all buyer constraints, so it is not directly comparable with
the belief-side `match_original`.

Known limitation left as-is by decision: `alignment_dict` still prefers the LLM's highest-confidence candidate field,
so `size_sqmm` chose `size_sq_mm` (pool support 1) over `size` (pool support 59). This keeps many size cells
unscored, but changing the selection order is out of scope for D088.

Tests after the extension: `tests/dialogue/test_d088.py` 6 passed; `tests/dialogue tests/matching` 467 passed. The
full suite is 1651 passed, 1 skipped, 2 failed only because the user-expanded `data/raw/final_constraints.csv`
changes the recorded mock-fixture key for two pilot-fixture tests; the live pipeline is unaffected.

## D089 — Unlisted product categories default to one persona (2026-11-04)

**Decision (user):** keep the five pilot categories at their tuned persona counts and make every other
category default to **1** persona instead of 2. `gtgen.llm.strata.personas_for_category` now returns:

- `Submersible Cable` → 2 (`GEM/2019/B/209245`)
- `Woollen Blanket` → 1 (`GEM/2019/B/231682`)
- `Submersible Pump` → 1 (`GEM/2019/B/242710`)
- `Sluice Valve` → 2 (`GEM/2019/B/341930`)
- `Laptop / Office Bag` → 3 (`GEM/2020/B/488257`)
- everything else, including `DI Fittings` (`GEM/2020/B/626099`) → 1

The pump branch still wins for compound names such as `Submersible Pump Cable`, so that case remains 1.
Test: `tests/llm/test_strata.py::test_personas_for_category_keeps_pilot_categories_and_defaults_others_to_one`.

## D090 — `bids.txt` allowlist and folder-based live runs (2026-11-04)

**Requirement (user):** a normal all-bids live run must be able to run from a data folder and must only run the
bids listed in `bids.txt` when that file is present.

`scripts/live_all_bids.sh` now accepts a data folder as its first positional argument (or `DATA_DIR=...`) and
exports `GTGEN_RAW_DIR` / `GTGEN_CANONICAL_DIR` so the CLI can read CSVs outside `data/raw`. It resolves an
allowlist in this order: explicit `BIDS_FILE`, `<folder>/bids.txt`, repo-root `bids.txt`, then a non-empty
`<folder>/bid_ids.txt` legacy alias. A non-empty allowlist is authoritative; an explicitly supplied or discovered
empty `bids.txt` is an error. With no allowlist, the runner falls back to `METADATA` (default
`<folder>/final_constraints.csv`). `BIDS_FILE=-` deliberately disables the allowlist, `BIDS=...` overrides it, and
`DRY_RUN=1` prints the resolved bid list without making LLM calls.

The direct CLI also gained `--data-dir` and the path env overrides `GTGEN_RAW_DIR`, `GTGEN_CANONICAL_DIR`,
`GTGEN_OUT_DIR`, and `GTGEN_LLM_CACHE`. The one-bid-per-process and `--personas 1` rules (D080/D084) are unchanged.
The operator-facing instructions are in the root `README.md`.
