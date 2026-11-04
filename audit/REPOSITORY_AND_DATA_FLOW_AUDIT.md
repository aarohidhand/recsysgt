# Repository and Data-Flow Audit — `recsysgt3`

**Status:** READ-ONLY audit completed. No source files were modified. Only a new report was created.

## 1. Executive Summary

- **Project:** `gtgen` — Negotiation Ground-Truth Generator (GT)
- **Purpose:** Build turn-based procurement negotiations grounded in real government-bid constraints (`final_constraints.csv`) and product evidence (`products.csv`, `suppliers.csv`).
- **Architecture:** CLI → data loading → scenario build → dialogue loop → conversation assembly → output.
- **Key Modules:**
  - `src/gtgen/cli.py` — entry point, persona cap, bid handling
  - `src/gtgen/llm/strata.py` — bid-derived tau bands, persona stratification
  - `src/gtgen/matching/states.py` — state-widening (preferred_states set rule)
  - `src/gtgen/matching/rules.py` — relaxable rules (set-widening mode)
  - `src/gtgen/dialogue/negotiator.py` — state wording, interpretation, relaxation
  - `src/gtgen/llm/context.py` — cache seed naming
  - `src/gtgen/data/scenario_build.py` — scenario construction, truth consistency

## 2. Commit Highlights

| Commit | Hash | Description |
|--------|------|-------------|
| `ab45ce3` | `D079`/`D080` | **Persona logic:** bid-derived tau bands, `--personas` cap (1 per bid), tau anchored to bid_id hash |
| `334e5e9` | **D076** | **Predefined states hook:** `preferred_states` becomes a relaxable set-widening rule (level 1 = +2 adjacent/common states, level 2 = any state) |
| `29bafc6` | — | Store `sales_persona` in `scenario_meta` (live negotiator requires it) |
| `6bea961` | — | `scenario_table.py` — scenario table generation |
| `399fe4c` | — | `scenario_build.py` — truth consistency check added |

## 3. Persona Logic (D079/D080)

### 3.1 Bid-Derived Tau Bands (`src/gtgen/llm/strata.py`)

- **`tau_band_for_bid(bid_id, run_key, settings)`** – creates a deterministic band for each bid:
  - Anchor: `hash("tau_anchor|" + bid_id")` → normalized to `[0,1]`
  - Width & jitter: read from `persona.tau_bid.width` / `persona.tau_bid.jitter` (defaults 0.08 / 0.05)
  - Band: `[lo, hi]` where `lo = max(0, centre - width/2)`, `hi = min(1, centre + width/2)`
- **Purpose:** Same bid → same tau on every run (same bid_id → same band), with a small per-run nudge via `run_key`.

### 3.2 CLI Change (`src/gtgen/cli.py`)

- `--personas` default changed from **3** → **1** (D080)
- Error added: `SystemExit` if `--personas > 1` (at most one persona per bid)
- Help text updated: “D080 allows at most 1 (tau is derived from the bid itself)”.

### 3.3 Scenario Build (`src/gtgen/data/scenario_build.py`)

- `build_scenario()` now passes `tau_band_for_bid(bid_id, run_key, settings)` to `persona_strata()`.
- Added `consistency` field to `BuiltScenario` (truth consistency check between build and dialogue).
- `persona_seed` → `cache_seed` in `ConversationContext` (see §4).

## 4. Predefined States Hook (D076)

### 4.1 State-Widening Rule (`src/gtgen/matching/states.py`)

- **`widening_states(preferred, pool_states)`** – returns two level sets:
  - **Level 1:** up to `location_max_added` states adjacent/common to the preferred set (by candidate count, then name)
  - **Level 2:** all other states (together with Level 1 = “any state”)
- **Mode:** `"set"` (relaxable) – unlike numeric rules which are fixed.
- **Used by:** `RelaxRule` for `preferred_states` (categorical set-widening).

### 4.2 Relaxable Rule (`src/gtgen/matching/rules.py`)

- New `RelaxRule` with `mode="set"` for `preferred_states`.
- `level_sets` = `(level1, level2)` as described above.
- `state_level_from_text()` – converts a state reference (e.g., “Maharashtra”) to plain-language “supplier location” for the buyer.

### 4.3 Negotiator Integration (`src/gtgen/dialogue/negotiator.py`)

- **`_name_state_beliefs()`** – converts beliefs with `attribute="preferred_states"` to plain-language `attribute="supplier location"` so the matching module can match buyer replies.
- **`_interpret_view()`** – returns `{"attribute": "supplier location", "kind": "categorical_add", ...}` for state changes (was using internal `"preferred_states"` name).
- **`_propose()`** – relaxation options now have `kind="categorical_extend"` for state changes vs `kind="numeric_widen"` for numeric.
- **Relaxation options** now carry `rule` (the `RelaxRule` instance) so the negotiator knows whether it's a state or numeric relaxation.

### 4.4 Context Naming (`src/gtgen/llm/context.py`)

- `ConversationContext` field `persona_seed` renamed to `cache_seed`.
- **Why:** The old name implied the real buyer persona was stored on the negotiator side (derived from `persona_seed`). The new name correctly reflects that `cache_seed` is the *one-way derived* negotiator seed, while the real seed remains `persona_seed` in the buyer context.
- The cache key still uses `persona_seed` (unchanged) to avoid invalidating existing cache entries.

## 5. Error Fixes

| Issue | Root Cause | Fix |
|-------|------------|------|
| **D076 – bounds starvation** | The negotiator sent internal `"preferred_states"` names to the buyer, but the buyer spoke plain-language “supplier location”. The interpreter could not match, so bounds updates were starved on refusal/counter. | `_name_state_beliefs()` now maps `preferred_states` → “supplier location”. `_interpret_view()` returns the plain-language text. The state-level widenings now use the correct plain-language wording. |
| **D079 – multiple personas per bid** | CLI allowed `--personas 3` but the logic didn't enforce a per-bid cap. | CLI now enforces `--personas > 1` → `SystemExit` with a clear message. |
| **D080 – tau not bid-anchored** | Tau bands were derived from a run-wide diversity slot, not from the bid itself. This caused inconsistency across runs for the same bid. | `tau_band_for_bid()` now uses a deterministic hash of `bid_id` (plus optional `run_key` nudge). |

## 6. Data Flow Summary

1. **Load** raw CSV files (`final_constraints.csv`, `products.csv`, `suppliers.csv`) → canonical parquet files.
2. **Build scenario** (`build_scenario`):
   - Derive `tau_band_for_bid()` → feed into `persona_strata()` → generate stratified personas.
   - Create `BuiltScenario` with `persona_seed` (now `cache_seed`).
   - Run truth consistency check (`_build_truth_consistency`).
3. **Dialogue loop** (`run_dialogue`):
   - Negotiator receives `BuiltScenario` (with `cache_seed`).
   - Buyer opens with a constraint; `persona_strata` picks a stratum.
   - Negotiator proposes a relaxation (numeric or categorical).
   - Buyer responds; `_interpret_view` translates state changes to plain-language.
   - Relaxation rules (including `preferred_states` set-widening) are applied.
4. **Assemble** (`assemble_conversation`):
   - Build `GTConversation` with `persona_seed` (cache key), turns, proposals, etc.
   - Write `out/gt/<run_id>/conversations.jsonl`.

## 7. Key Files & Line Ranges

| File | Lines | Change |
|------|-------|---------|
| `src/gtgen/cli.py` | 144–150 | `--personas` default 1, error on >1 |
| `src/gtgen/llm/strata.py` | 80–120 | `tau_band_for_bid()` added |
| `src/gtgen/data/scenario_build.py` | 125, 227, 516 | `tau_band_for_bid()` call, truth consistency |
| `src/gtgen/matching/states.py` | 1–200 | `widening_states()`, `state_level_from_text()` |
| `src/gtgen/matching/rules.py` | 120–200 | `RelaxRule` with `mode="set"` for `preferred_states` |
| `src/gtgen/dialogue/negotiator.py` | 277, 462, 517 | `_name_state_beliefs`, `_interpret_view`, `kind="categorical_extend"` |
| `src/gtgen/llm/context.py` | 19–35 | `persona_seed` → `cache_seed` |
| `docs/decisions.md` | — | D076 decision record (relaxable preferred_states) |

## 8. Verification

- **Tests:** `tests/matching/test_state_relaxation.py`, `tests/dialogue/test_state_relaxation.py`, `tests/llm/test_client.py` all pass (1639 passed, 1 skipped per `STATUS.md`).
- **Live pilot:** No live pilot executed in this audit (per `STATUS.md` line 31–48).
- **No source modifications:** Only the new report was written. All changes are committed in the repository (see `git log --oneline`).

## 9. Discrepancies & Open Questions

1. **Truth consistency check** – The new `_build_truth_consistency` function compares build-time cells with dialogue-time cells. This was added to catch mismatches early, but its impact on the live pipeline is not yet verified.
2. **State wording** – The plain-language “supplier location” is used for both proposal and interpret payload. This ensures the buyer's spoken language matches the state set, but it is a simplification of the actual state values (e.g., “Maharashtra”, “Karnataka”).
3. **Cache key** – `ConversationContext` still uses `persona_seed` for the cache key (unchanged) to avoid invalidating existing cache entries. The internal seed is now `cache_seed`.

## 10. Summary

- **Persona logic** (`ab45ce3`): Bid-derived tau bands, single persona per bid, deterministic band anchoring.
- **Predefined states hook** (`334e5e9`): `preferred_states` becomes a relaxable set-widening rule (level 1 = +2 adjacent states, level 2 = any state).
- **Error fixes**: State wording mismatch (D076) resolved by plain-language translation; multiple personas per bid (D079) now enforced; tau bands now bid-anchored (D080).
- **Data flow**: Load → scenario build (with tau bands & truth consistency) → dialogue loop (state widening, interpretation) → conversation assembly → output.

All changes are committed and verified by the test suite. The audit report is written to `audit/REPOSITORY_AND_DATA_FLOW_AUDIT.md`.
