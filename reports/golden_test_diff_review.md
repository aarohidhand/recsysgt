# Review of the rewritten golden / policy tests (fec4412 -> working tree)

Reviewer: gt-auditor (read-only; nothing outside `reports/` edited). Baseline: `python -m pytest -q` -> 839 passed.
Mutations were applied in memory only, through a pytest plugin in the scratchpad
(`/tmp/claude-1000/-home-nyxelray-Pythonstuff-recsysgt3/a3bcd410-7b75-4665-bea2-a63bf1d42b51/scratchpad/mut.py`, runner `run.sh`,
selected by env `MUT=<name>`). It patches `gtgen.dialogue.buyer.buyer_decide`, `gtgen.matching.buyer_decision.*`,
`gtgen.matching.front.domain_levels`, `build_ranking_matrix` in the dialogue modules. No file under `src/` or `tests/` was touched.

## Verdict

PASS WITH ISSUES. The three rewrites do test the D053 buyer and loop, and the arithmetic I recomputed independently is right. Every
mutation of the D053 branches I tried is caught, except the ones listed under "Weakened". I found no real barrier leak and no dropped
HARD / UNKNOWN / pending / determinism invariant. Findings: 4 items weakened (3 in rankings / state / barrier strength of
`test_golden_default_settings.py`, 1 vacuous assertion in `test_policy.py`), 1 test that checks nothing, 2 minor gaps that already existed.

## 1. Test by test

Verdict key: equivalent / replaced-equally-strong / weakened / not-checking.

### tests/dialogue/test_policy.py (11 tests)

| Test | Old behaviour | New assertion (rule / code path) | Mutation evidence | Verdict |
|---|---|---|---|---|
| test_hard_never_yes | HARD -> NO for lenient tau 0 and strict tau .9 | same + reason `policy`, `truth.bounds == {}`, `rng.n == 0` (HARD is not a rule: no bound, no draw). Path: `respond_to_proposal` puts the id in `not_permitted` -> `buyer_decide` step 3 | `hard_ignored_both` (hardness stripped AND not_permitted emptied) -> FAILS. `hard_ignored_only_hardness` -> passes: two layers guard it and this test only fails if both break. The `buyer_decide` HARD branch itself is pinned by `tests/matching/test_buyer_bounds.py::test_decision_branches` | replaced-equally-strong (slightly stronger) |
| test_doc_proof_bypass | doc_to_pending item, strict -> BYPASS, doc_name given to the LLM | same + `bounds == {}`, `state.bypassed == ("C3",)`. Path: doc_proof item -> step 2 BYPASS | `doc_proof_no` -> FAILS | equivalent+ |
| test_lenient_and_medium_never_counter | cost sweep 0..1 in steps of .05, counter_band 1.0 | rng sweep u in {0,.5,.99} x levels {1 (inside T), 2 (beyond T)} with T=1. Level 2 is the exact case where a strict buyer would COUNTER, so it exercises the tier gate | `counter_all_tiers` -> FAILS (5 tests) | replaced-equally-strong |
| test_strict_counter_numeric_at_its_own_limit | strict counters at cost a (fraction .5 -> "20") | 15 -> 19.5 is level 2 > T=1: COUNTER at level 1 = 17.25, no draw, bound `{"max": 1}`. Path: step 5, `scale_value` ratio levels[1]/levels[2] | `counter_never`, `tolerance_ignored`, `counter_as_yes` -> FAIL. Value 17.25 = 15 x 1.15, consistent with RELAXATION_LOGIC 11.3 step 5 (COUNTEROFFER at min(T_c,L_c)) and N3 (max k) | replaced-equally-strong |
| test_counter_not_for_a_rule_that_cannot_be_relaxed | strict + categorical_extend -> NO | categorical rule is not in `truth.by_ref`; -> NO, reason `policy` (D053: only numeric inequalities relax) | `not_permitted_ignored_only` -> FAILS | replaced-equally-strong |
| test_yes_within_tolerance_and_no_beyond | cost <= a -> YES, > a -> NO | level 1 <= T -> draw (u=0 -> YES, `rng.n==1`, bound `fixed 1`); level 2 > T for MEDIUM -> NO `tolerance`, no draw, bound `max 0` | `tolerance_ignored`, `never_yes`, `counter_all_tiers`, `no_not_pinned` -> FAIL. NOTE: comment says "medium_strict" but the code uses `Tier.MEDIUM` with tau .5 (a tier/tau pair the tier binning would not produce; harmless) | replaced-equally-strong |
| test_category_tolerance_scales_acceptance | tolerance multiplier scales a | tolerance in levels: T=0 -> NO, T=1 -> YES at level 1 | `tolerance_ignored` -> FAILS | replaced-equally-strong |
| test_partial_acceptance_mixed | [NO, BYPASS, YES], MIXED/PARTIAL | same + counter_value None | `hard_ignored_both`, `doc_proof_no`, `never_yes` -> FAIL | equivalent |
| test_relaxation_limit_replaces_the_per_turn_concession_cap | cap=1 flips lowest-gain YES to NO, qa flag | `max_relaxations=1`: [YES, NO], reason `limit`, bounds `{C2 fixed 1, C4 max 0}` | `limit_ignored`, `limit_off_by_one` -> FAIL. BUT the second half ("the cap does not reset per turn") is vacuous: C4 already has bound max 0, so turn 2 returns `bounded`, not `limit`. `limit_reset_per_turn` (accepted_rules emptied) -> all 839 tests pass | weakened (cross-turn part only) |
| test_unmatched_belief_yes_not_counted | unmatched belief YES not subject to cap | unmatched B9 + B2 with `max_relaxations=1` -> [YES, YES], flag `unmatched_belief:B9`, item label | not mutated (no cheap seam) | equivalent |
| test_counter_value_is_a_point_on_the_way_... | `counter_value_for` band shapes | tests `scale_value` shapes ("10"->"8..12" at .25 -> "9.5..10.5" etc.) | it is a helper test; the buyer-level counter is pinned by test_strict_counter (scalar) and the golden runs (10.5, 11.5). A band-target counter through `respond_to_proposal` is not tested, as before | replaced-equally-strong (helper level, same coverage as before) |
| test_opening_records_reveals | unchanged | unchanged | - | equivalent |

Not carried from the old file and not required by D053: concession-cap qa flag, `a=(1-tau)*w` boundary (0.5 vs 0.51), counter_band.
The probabilistic acceptance formula is not exercised in these three files at all (u=0 everywhere); `always_yes` and `tau_ignored`
mutations pass them. They ARE caught elsewhere (`tests/matching/test_buyer_bounds.py::test_accept_probability_hand_computed`,
`test_probabilistic_accept_uses_the_seeded_draw`, `tests/dialogue/test_relaxation_loop.py::test_unit_strict_counters_at_tolerance_and_yes_needs_the_draw`
which pins reason `declined`). So not a gap in the suite, only in this file's old "boundary" test, which has a replacement elsewhere.

### tests/dialogue/test_golden_default_settings.py (fully rewritten)

| Test | Old behaviour | New assertion | Mutation evidence | Verdict |
|---|---|---|---|---|
| test_settings_are_the_shipped_defaults | pinned some settings | pins levels `[0,.02,.05,.10,.25,.50]`, max_dev .25, `level_mode: relative`, `config_from_settings` levels, `allow_hard is False`, proof rate strict .5, shortlist 3, bundle 4 | would fail if the engine silently falls back to defaults (the 6eb11b5 failure mode) | replaced-equally-strong |
| test_action_sequence_is_the_hand_computed_one | scripted 4-item bundle script | 15-turn action sequence (propose/MIXED/ACK x3, CLARIFY, REVEAL, CLARIFY, REVEAL, RECOMMEND), revealed ids per turn (reveal_check) | `counter_never`, `counter_at_r_minus1`, `tolerance_ignored`, `front_ignores_bounds`, `never_yes` -> FAIL | replaced-equally-strong |
| test_property_1_multi_item_proposal | 4-item near-miss bundle | 2-item proposal (B3 100->98, B5 10->11), joint gain 1, chosen point `{B3:1,B5:3}`, `agrees_with_reference`, the whole 3-step front with costs 1.0/1.5/1.75 | independently re-derived: F1=2%, D3=10%, W3=10%, cost = sum r/4 -> 1.0, 1.5, 1.75 correct | replaced-equally-strong |
| test_property_2_counter_from_a_strict_buyer | none (counter at cost a) | MIXED/PARTIAL, B3 YES, B5 COUNTER at 10.5 (= 10 x 1.05, level 2), decision record `reason tolerance`, bounds truth `{C3 fixed 1, C5 max 2}`, every later proposal for B5 equals 10.5 and every later front point has B5 level <= 2 | `counter_never`, `counter_at_r_minus1`, `front_ignores_bounds`, `front_ignores_max_only` -> FAIL | replaced-equally-strong (new, stronger on D056 "counter -> next proposal <= counter level") |
| test_property_3_bypass_proof_request_changes_no_rank_or_score | n/a (old scenario had BYPASS through a bundle) | with vs without proof requests: 3 proof requests (turn, product, attribute, doc), BYPASS item at the end of the buyer turn, `ACKNOWLEDGE` names the doc, rank/score/p_feasible/utility equal, ALL ranking rows byte-equal after clearing `pending_verification_docs` (reference, initial, negotiator), compliance detail equal, top1 match/perfect equal, front/bounds/accepted unchanged | `pending_penalised` (x0.9) and `pending_zero` -> FAIL (in the negotiation golden; the default-settings golden catches them through the same strip-compare) | replaced-equally-strong; this is the strongest test in the three files (D055 acceptance test as written) |
| test_property_4_at_least_three_relaxation_iterations | none | 3 proposals + 6 rows total, chosen points, verified counts truth/belief (1->1, 1->2, 2->3), bounds_after truth AND belief per row (negotiator re-derived bounds from the transcript), accepted rules never on later fronts | `front_ignores_*` -> FAIL | replaced-equally-strong (D056 acceptance test) |
| test_accepted_relaxations_are_levels_of_the_original_value_with_gains | partly | 98 / 21.6 / 10.5 = 2% / 10% / 5% of the ORIGINAL values, truth_gain and estimated gain, `gain_estimate_error==0`, `belief_bounds_accuracy==1` | - | replaced-equally-strong |
| test_counts_and_rankings | `_check` of full tables REF_INITIAL, REF_FINAL, NEG_FINAL (rank, hard/soft counts, p_feasible, utility, score, match_original per row, verification_required per row, sum p_recommend == 1) | order of reference ranking; p_feasible dict; scores of P4/P3/P1 only; initial ranking only as the set with p_feasible == 1; negotiator top-3 order; verification_required for eligible rows; top1 match 5/7 and perfect deal | - | **weakened** (see W1) |
| test_belief_ids_are_stable_and_originals_never_compound | none | belief ids stable, final values from ORIGINAL 100/24/10 | - | new, fine |
| test_barrier_negotiator_inputs_hold_no_truth_bounds_or_persona | regex list (incl. `persona`, `item_responses`, `counter_band`, `patience`, `max_concessions`, `synthetic_commercial`) | regex list (adds tolerance, max_relaxations, patience_rounds, decision_randomness, `"fixed"`, `"max"`, strict, truth id pattern; **drops** `persona`, `item_responses`, `counter_band`, `patience` (only `patience_rounds` remains), `max_concessions`, `synthetic_commercial`) | scan with all `Persona.model_fields` names found no leak (see section 3), so no current leak | **weakened** (see W3) |
| test_deterministic | equal model_dump | equal model_dump_json (rng patched to constant 0.0). I also ran the same scenario twice with the REAL seeded rng: identical, and a different conversation (`... PROPOSE, NO, BEST_AVAILABLE`), so the u=0 path is a deliberate pin, not luck | - | equivalent+ |
| test_printed_conversation_shows_the_four_properties | n/a | printer works and shows multi-item, COUNTER, 3 BYPASS, 3 iterations | - | fine (documentation test) |
| test_d059_item_response_id_spaces_are_consistent | n/a | truth ids in `constraint_ref`, belief ids in `belief_ref`, proof rows have no belief_ref, lines up with the previous proposal | - | new, good |

Dropped from the old file without replacement: per-turn `buyer_truth_state_after` evolution (old test "T4 ... truth evolution"), see W2.

### tests/dialogue/test_golden_negotiation.py

| Test | Old behaviour | New assertion | Mutation evidence | Verdict |
|---|---|---|---|---|
| test_opening_states_all_six_constraints | same | same | - | equivalent |
| test_step1_proposal_is_the_cheapest_front_point | 2-item near-miss bundle, joint gain 4 | 1-item proposal flow 100->85, gain 2 (P6,P7), front `[{B5:1} 0.5/2, {B4:1,B5:1} 1.0/3]`, chosen = reference, no HARD belief in a proposal, canonical belief ids | I re-derived the front by hand (F1 -> P6,P7 at .5; W1 -> P8 dominated; {F1,W1} -> 3 suppliers at 1.0) correct | replaced-equally-strong |
| test_turn2_zero_tolerance_means_no_and_the_rule_never_comes_back | counter/accept mixed | tolerance 0 -> NO `tolerance`, bound max 0 truth and belief, flow never on a later front or proposal | `no_not_pinned` -> FAILS | replaced-equally-strong |
| test_turn3_and_4_warranty_is_proposed_and_accepted | n/a | warranty 24->20.4 (15% = L1), gain 1 (P8), front after flow ban, YES, `fixed 1`, p = sigmoid(5.4(1-.75-.5)) = .206 > u=0 | re-derived | replaced-equally-strong |
| test_turn5_and_6_counter_sets_a_bound_the_negotiator_respects | counter-counter | delivery 10->13 (L2), strict COUNTER at L1 = 11.5, bound max 1 truth+belief, next front empty -> BEST_AVAILABLE; whole action list | `counter_never`, `counter_as_yes` (crashes via validate_transition), `front_ignores_*` -> FAIL | replaced-equally-strong |
| test_turn7_best_available_with_the_offer | n/a (old ended in RECOMMEND) | BEST_AVAILABLE, reason `no_feasible`, items P1 1.0, P8 .875 [warranty], P6 .875 [flow], feasible counts per turn truth/belief | match_original re-derived (7/8 with HARD weight 2: total 8) correct | replaced-equally-strong |
| test_final_outcome_rankings_and_accepted_relaxations | REF/NEG rows | full row tables REF_ROWS / NEG_ROWS (rank, hard/soft counts, pending doc, p_feasible, utility, score), p_recommend sums to 1, accepted = warranty only, compliance detail 48 cells, P3 delivery stays FAIL (counter never agreed), P8 warranty PASS, belief P4 warranty PASS | `pending_penalised`, `pending_zero`, `hard_unknown_pass` -> FAIL | replaced-equally-strong. New invariant worth keeping: a COUNTER does not change truth state |
| test_pending_verification_is_not_penalised_rule_1_7 | forced-PASS comparison | unchanged | mutation x0.9 -> FAILS | equivalent |
| test_barrier_negotiator_payloads_and_step_only_driving | regex incl. `persona` | regex adds tolerance / max_relaxations / `"fixed"` / `"max"`; `persona` becomes `(?<!sales_)persona(?!_seed)`; consecutive prefixes only via `step`; call-count [4,3,3] | see section 3 | equivalent / see W3 |
| test_scenario_is_deterministic_and_fast | same | same | - | equivalent |
| test_missing_negotiation_unclear_limit_raises | parametrized over `max_counter_rounds`, `unclear_limit` | `unclear_limit` only | - | equivalent (the other key is removed by D057) |
| test_max_counter_rounds_is_no_longer_read | KeyError when the key is missing | pops a key that `config/settings.yaml` no longer has (`grep` finds it only in the test), compares a run to itself apart from provenance | it can only fail if the code re-reads the key, and then every golden would also fail with KeyError | **not-checking** (vacuous; see W4) |
| test_counter_bound_is_read_by_the_negotiator_from_the_transcript | `max_counter_rounds` setting read | negotiator fed ONLY transcript prefixes reproduces the belief bounds | good: it is the D045 "no side channel" property | replaced-equally-strong |
| test_unclear_limit_is_read_from_settings | scripted | UNCLEAR below limit -> item asked again (bounds `{}`), at limit -> `max 0`; dead variable `refs` | - | equivalent |
| test_bypass_turns_and_policy / test_bypass_keeps_candidate_lists_doc_and_does_not_penalise / test_bypass_scenario_barrier (scenario 2) | BYPASS through a `categorical_extend` proposal on a doc-proof truth constraint | D055 proof request: YES + BYPASS item on the same constraint, `proof_requests` fields, `buyer_reply` receives a PROOF_REQUEST item with the doc name, ranking rows byte-equal apart from pending docs vs the proof-free run, compliance detail equal, forced-PASS equality | the old BYPASS-through-proposal path (rule None + doc_proof) is no longer an end-to-end golden, but is pinned by `test_policy::test_doc_proof_bypass` and `test_relaxation_loop::test_bypass_on_a_doc_proof_item_keeps_front_and_bounds` (`doc_proof_no` is caught there) | replaced-equally-strong |

## 2. Anything weakened, with a concrete fix

W1. `test_golden_default_settings.py::test_counts_and_rankings` lost the exact ranking tables. The old file asserted, for every row of
`reference_ranking_initial`, `reference_ranking` and `negotiator_ranking`: rank, hard/soft pass/fail counts, p_feasible, utility, score,
match_original, `verification_required`, and that p_recommend sums to 1. The new test checks ordering, p_feasible, three scores, the
initial ranking only as a set `{P1}`, and only the top-3 of the negotiator ranking. P6, P2, P5 utility/score, `reference_ranking_initial`
order and scores, p_recommend and per-row match_original are unpinned in this file. (`test_golden_negotiation` does pin full rows for its
scenario, except `reference_ranking_initial`, `match_original` per row and p_recommend values, as before.)
Fix: restore a `_check(rows, expected)` helper with REF_FINAL / REF_INITIAL / NEG_FINAL tables for scenario 4 (the docstring already gives
the formulas: persona weights spec .5 price .3 rating .2, price_score = (52000 - p)/11000 over eligible rows, rating_bayes = ((10x3.8 + 10r)/20)/5)
and assert `sum(p_recommend) == 1` and `match_original` per row.

W2. Per-turn state consistency is no longer asserted. The old default golden asserted `buyer_truth_state_after` after the concession turn;
neither golden checks `Turn.buyer_truth_state_after` now (only `test_negotiator_loop.py` line 182-183 and `test_best_available.py` line 212 do).
That field is exactly what audit check 5 ("truth state after each turn = previous + accepted change only") reads, and the COUNTER turn
(T6 in the negotiation golden, T2 in the default golden) is the one where an engine bug would put the counter value into the truth state.
Fix: in `test_golden_negotiation` assert per turn that the truth value of TRUTH-C4 is "24" before turn 4 and "20.4" from turn 4 on, and
TRUTH-C3 / TRUTH-C5 stay "10" / "100" through turn 7 (including after the COUNTER at turn 6 and the NO at turn 2); in the default
golden assert C3 98 from turn 2, C5 10 at T2 (counter only a bound), 10.5 only after turn 8.

W3. Barrier regex (task item c). Summary: no real leak is hidden today, but the net strength dropped.
 - `(?<!sales_)persona(?!_seed)` in test_golden_negotiation exempts `persona_seed` and any `sales_persona*`. The exemption is needed
   because the fake runners record `ctx` kwargs (`ConversationContext.persona_seed`, D054), and the real client uses `ctx` only for the cache key and the log
   (`llm/client.py` lines 189-194 and 327-328; `_voice_nonce` is used only by buyer prompts, runners.py lines 588 / 607 / 772). I confirmed with a scan using every
   `Persona.model_fields` name that the ONLY persona-field hit in any negotiator payload of both goldens is `persona_seed`, in `ctx`.
   `persona_seed` is the key of the buyer's random stream (`buyer_rng(persona_seed)` = `Random(f"{seed}:buyer")`), so a negotiator that read it could replay
   every buyer draw. The regex now cannot see that if the value ever reaches a prompt variable, and I checked with regex probes that `{"persona_seed": 7}`,
   `{"sales_persona_seed": 7}` and `"persona_seed_for_buyer_rng": 7` are all not flagged; `persona_tau`, `buyer_persona`, `persona` ARE flagged.
 - `test_golden_default_settings.py` regex dropped `persona`, `item_responses`, `counter_band`, `patience` (plain; `patience_turns` is still a Persona
   field and `patience_rounds` does not match it), `max_concessions` (field `max_concessions_per_turn` still exists in the contract) and `synthetic_commercial`.
   Hidden-field names `counter_band`, `patience_turns`, `max_concessions_per_turn`, `item_responses`, `persona` can therefore appear in a negotiator payload of the
   default golden unflagged.
 Fix: build the forbidden set from the contract instead of hand-typing it: for each of `Persona.model_fields` (except `persona_seed`),
 `BuyerConstraint` hidden fields (`golden_param`, `scenario_role`, `evidence_coverage`, `provenance`, `merged_from`, `framing_decision`, `bid_id`), plus
 `ItemResponse` / `item_responses`, assert `re.search(rf"(?<![A-Za-z_]){name}(?![A-Za-z_])", blob)` is None; scan the payload with
 `kwargs["ctx"]` removed (so `persona_seed` is allowed ONLY under `ctx`), and add one assertion at the client boundary that the rendered messages of
 every `negotiator_*` prompt do not contain `str(persona_seed)` as a token. Also share one constant between the two files so they cannot drift.

W4. `test_golden_negotiation.py::test_max_counter_rounds_is_no_longer_read` checks nothing (the key is already deleted from `config/settings.yaml`; `pop(..., None)` is a
no-op, then a run is compared with an identical run). Fix: delete it, or replace it with `assert "max_counter_rounds" not in settings["negotiation"]`
plus a source grep test; the case "code re-reads the key" is already caught by every golden raising KeyError.

W5. `test_policy.py::test_relaxation_limit_replaces_the_per_turn_concession_cap`, second half: the claim "the cap does not reset per turn" is not tested
(see the table: `limit_reset_per_turn` passes all 839 tests; the wiring `accepted_rules = [k for k,b in bounds.items() if "fixed" in b]` in
`respond_to_proposal` is covered by no dialogue test; `buyer_decide`'s own cross-call limit is covered in `tests/matching`). Fix: in turn 2 propose an item the buyer
has not seen before (e.g. a third SOFT rule), not B4 (already pinned to `max 0`), and assert `reason == "limit"`.

Pre-existing gaps, not caused by the rewrite (noted for the owner):
 - COUNTER for tier `medium_strict` is covered by no test in `tests/dialogue` or `tests/matching` (mutation `counter_strict_only` is caught only by two `tests/eval` tests). The old
   tests also exercised only STRICT. Add one case with `Tier.MEDIUM_STRICT` to `test_policy.py`.
 - No golden contains a HARD constraint with UNKNOWN evidence (P7's UNKNOWN is the SOFT certificate), so "HARD UNKNOWN never PASS" in the golden relies on
   `tests/ranking` and `tests/matching` (present). It was equally absent from the old goldens.
 - The `step`-only property is now tested with a spy that forwards `last_analysis`/`last_cells`; the pure step-only case remains in `tests/dialogue/test_barrier.py` (line 59-72),
   and `loop.py` reads those attributes with `getattr(..., None)`. Fine.

## 3. (b) Invariant check under D053

| Invariant | Still asserted? | Where |
|---|---|---|
| HARD never YES / never relaxed | yes | test_policy::test_hard_never_yes, test_partial_acceptance_mixed, golden step 1 ("never a HARD constraint"), `cfg.allow_hard is False` |
| UNKNOWN on HARD never PASS | not in the goldens (never present there, same as before); covered in tests/ranking, tests/matching | - |
| Information barrier | yes (weakened, W3) | both goldens |
| Pending verification not penalised | yes, strong | golden_negotiation rule 1.7 + strip-equality in the default golden and scenario 2 |
| No hardening on refusal (D045 #1) | **was in the old golden (line 454), dropped.** Still true today (I checked belief states of the negotiation golden: flow/delivery/warranty stay SOFT in turns 1-5) | add `assert all(b.believed_hardness == SOFT ...)` for the three soft attributes in every turn |
| Determinism | yes; I additionally ran the default golden twice with the real seeded rng: identical | golden tests use the constant rng |
| Hand-computed arithmetic | yes, see section 4 | |
| Compliance detail / truth state per turn | partly (W2) | |

## 4. (d) Independent recomputation

All numbers below were computed by me from the docstring tables only (a separate script, `settings.yaml` ranking constants: prior mean 3.8, prior count 10,
tenure cap 10, softmax T .15), not from the code under test.

REF_ROWS, persona weights spec .4 price .3 rating .2 location .1, price_score = (52000 - p)/11000, pool of the 7 eligible rows (P2 excluded: clipped to 1):
 - P8: spec 6/6, price 9/11, rating (38 + 8x3.5)/18/5 = .733333, Mumbai 0 -> .4 + .245455 + .146667 = **.792121**; p = .8 (cert row confidence) -> score **.633697**. Matches.
 - P7 (not hand-checked by the engineer): spec 4/6 (power, phase, delivery, warranty pass; flow FAIL; certificate has no row -> UNKNOWN), price 3/11, rating (38 + 200x4.7)/210/5 = .931429, Pune 1 ->
   .266667 + .081818 + .186286 + .1 = **.634771**; p = .3 x .5 = .15 -> **.095216**. Matches `util=0.63477056...`, `score=0.0952155...`.
 - P2: spec 4/6, price clipped 1, rating (38 + 30x4)/40/5 = .79, Chennai 0 -> .266667 + .3 + .158 = **.724667**; HARD fail -> score 0. Matches.
 - P3: spec 5/6, price 1, rating (38 + 60x4.2)/70/5 = .828571, Pune -> .333333 + .3 + .165714 + .1 = **.899048**; p .3 -> .269714. Matches.
 - P1: .4 + 0 + .2 x .907692 + .1 = **.681538**. Matches.

NEG_ROWS, default weights (spec .3, price .2, moq .1, rating .15, tenure .1, location .05 (no buyer city -> 0), evidence_confidence .1):
 - P1: .3 + 0 + .1 + .136154 + .09 + .1 = **.726154**. Matches.
 - P7: spec 4/6 -> .2, price 3/11 x .2 = .054545, moq 1 -> .1, rating .139714, tenure 10/10 -> .1, evidence 1 -> .1 = **.694260**; score x .15 = .104139. Matches.
 - P2: .2 + .2 + .1 + .1185 + .04 + .1 = **.7585**. Matches.
 - P8: evidence_confidence = (5x1 + .8)/6 = .96667 -> .790303. Matches `util=0.7903030...`.
 - P3: spec 5/6 -> .25, price .2, moq 1 (moq 10) .1, rating .124286, tenure .06, evidence .1 = **.834286**. Matches.
So P7 and P2 (the rows the engineer did not hand-check) are correct, and they were also in the pre-D053 hand-checked docstring (P7 util .634771).

Default golden spot checks: P4 util = .5 + .3 x 8/11 + .2 x .79 = .876182 (price_score pool min 41000 because P5 is the HARD fail), rating (38 + 10x4.1)/20/5 = .79. Matches. Level arithmetic:
F1 = 98, D3 = 11, W3 = 21.6, D2 = 10.5, costs (1+3)/4 = 1.0, (1+3+2)/4 = 1.5, (1+3+3)/4 = 1.75. Matches. p(T2 flow) = sigmoid(5.4 x (1 - .8 - .25)) = .433 > 0 = u. Matches.
Negotiation golden front: F1 -> {P6, P7}; W1 (20.4) -> P8; D2 (13) -> P3; P4 (14) and P5 (15) out of reach; after the flow ban and warranty fix the only option is D2, COUNTER at level 1 = 11.5, then nothing -> BEST_AVAILABLE. Matches.

## 5. Commands run (all from /home/nyxelray/Pythonstuff/recsysgt3)

```
python -m pytest tests/dialogue/test_policy.py tests/dialogue/test_golden_default_settings.py tests/dialogue/test_golden_negotiation.py -q
  -> 45 passed in 0.31s
python -m pytest -q
  -> 839 passed in 2.98s
$S/run.sh <mutation...>      # S = scratchpad; pytest -p mut, mutation chosen by env MUT, only the three files
  hard_ignored_both            -> 3 failed (test_hard_never_yes, test_counter_not_for_a_rule..., test_partial_acceptance_mixed)
  hard_ignored_only_hardness   -> 45 passed (see test_hard_never_yes row; tests/matching pins the buyer_decide branch)
  not_permitted_ignored_only   -> 1 failed (test_counter_not_for_a_rule_that_cannot_be_relaxed)
  counter_all_tiers            -> 5 failed (4 lenient/medium parametrisations, test_yes_within_tolerance_and_no_beyond)
  counter_never                -> failures in test_policy and 7+ golden_default tests
  counter_at_r_minus1          -> failures in golden_default
  doc_proof_no                 -> 2 failed (test_doc_proof_bypass, test_partial_acceptance_mixed)
  limit_ignored / limit_off_by_one -> 1 failed (test_relaxation_limit_...)
  tolerance_ignored            -> failures in test_policy x3 and golden_default
  never_yes                    -> failures in test_policy x5 and golden_default
  always_yes, tau_ignored      -> 45 passed in these three files (whole suite: caught by tests/eval and tests/matching: 6 resp. 1 failures)
  counter_as_yes               -> test_strict_counter... fails, goldens error (validate_transition raises)
  no_not_pinned                -> 3 failed (incl. test_turn2_zero_tolerance_means_no_and_the_rule_never_comes_back)
  pending_penalised            -> 3 failed (golden_negotiation); pending_zero -> 4 failed; hard_unknown_pass -> 2 failed
  front_ignores_bounds, front_ignores_max_only -> golden_default failures (action sequence, property 2, 3, 4, ...)
  limit_reset_per_turn         -> 45 passed; whole suite (839) passed -> unpinned wiring (W5)
  counter_strict_only (medium_strict removed from counter tiers), whole suite -> 2 failed, both in tests/eval
python $S/barrier_scan.py     # all Persona.model_fields + hidden BuyerConstraint names, as exact tokens, in both goldens' negotiator payloads
  -> only hit: persona_seed (in ctx kwargs); no tau / tier / tolerance / bounds hits; persona numeric values .8 .75 .25 absent
python $S/det.py              # default golden twice with the REAL seeded buyer rng
  -> real-rng identical: True
python $S/hard.py             # believed_hardness per turn of the negotiation golden -> all SOFT (no hardening)
python (regex probes of (?<!sales_)persona(?!_seed)) -> persona_seed / sales_persona / sales_persona_seed not flagged; persona_tau, buyer_persona, Persona flagged
```

Owners per CLAUDE.md section 5: all findings are in `tests/dialogue/` (dialogue-engineer). W3's client-boundary assertion touches `tests/llm/` (llm-engineer).
