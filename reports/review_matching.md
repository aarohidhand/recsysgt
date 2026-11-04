# Matching Module Code Review (gt-auditor, 2026-10-27)

Tests: `pytest -q tests/matching tests/ranking` → 60 passed in 0.08s.

| Rule | Verdict | Notes |
|------|---------|-------|
| 1. Near-miss definition (D025) | PASS | HARD FAIL/UNKNOWN excluded; SOFT UNKNOWN non-blocking; correct pool filtering |
| 2. Minimal bundles (D025) | PASS | Every item checked for marginal contribution ≥ min_marginal_gain |
| 3. Ranking order (D025) | PASS | Sort key matches spec exactly |
| 4. Refused-item exclusion | **FAIL** | No `excluded` parameter in search/select/nearest_gain_value |
| 5. HARD never relaxable (§1.6) | PASS | `_soft_refs` filter; explicit check in nearest_gain_value |
| 6. PENDING_VERIFICATION not penalised (§1.7) | PASS | Same p_c as PASS in feasibility and ranking |
| 7. Compliance statuses (§1.7) | PASS | All four produced and tested correctly |
| 8. Test coverage | PARTIAL | 5 missing test scenarios identified |

**Counts: 6 PASS, 1 FAIL, 1 PARTIAL**

## FAIL: Refused-item exclusion (rule 4)

`search()`, `search_bundles()`, `select_bundle()`, and `nearest_gain_value()` have no
`excluded` parameter. The negotiator can re-propose constraints the buyer already rejected.

**Fix:** Add `excluded: Optional[set[str]] = None` to these functions and filter out
excluded refs from `_soft_refs()` and bundle subsets. Alternatively, the dialogue layer
filters before calling. Owner: matching-engineer and/or dialogue-engineer.

## PARTIAL: Test coverage (rule 8)

Missing cases:
1. SOFT UNKNOWN + SOFT FAIL in relaxation (relaxing FAIL makes it feasible despite UNKNOWN)
2. Refused-item exclusion (once feature exists)
3. Four-item bundle at max_bundle_size=4
4. Empty candidate list edge case
5. doc_to_pending in a mixed bundle

## Recommendations

1. The dialogue-engineer should filter refused refs before calling select_bundle. This is
   the simplest fix and doesn't require matching-engineer changes.
2. Evidence dataclass allows lo without hi; add an invariant check.
3. check_owner.py doesn't recognise worktree-prefixed paths for the gt-auditor.
