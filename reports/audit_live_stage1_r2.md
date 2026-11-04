# Audit: live pilot r2, run pilot_20261031_213936_f0961b67

Scope: out/gt/pilot_20261031_213936_f0961b67/conversations.jsonl (2 conversations: 488257 strict tau 0.942, 341930 lenient tau 0.185; 8 turns each).
Offline only. No network, no --live, no LLM calls. Compared with reports/audit_live_stage1.md.

## Verdict: FAIL (improved over r1, but the core defects remain)

Fixed since r1: final turns are now LLM-generated (negotiator_final in llm_call_ids and in the provenance prompt_versions); zero-PASS demotion is applied and recorded; the CLARIFY step runs before BEST_AVAILABLE; the sales persona differs per scenario; there are no template strings, tags, GeM terms or spoken arithmetic.
Not fixed: (1) no relaxation was ever proposed, (2) the BEST_AVAILABLE recommendation is poor and ordered wrongly, (3) the final turns contain unsupported "does not meet" claims, (4) belief extraction misaligns constraints, (5) the scenarios are still unsatisfiable (initial_feasible = 0 in both).

## Commands run (real output)
- `python -m gtgen.cli eval --run pilot_20261031_213936_f0961b67`: hard_violation_rate_recommended 0.333, best_available_share 1.0, ndcg/mrr/map 0.0, realism verdict FAIL (flags: 488257 opening_greeting; 341930 opening_sentences "6 sentences (allowed 2..5)"), persona diversity PASS.
- `python -m pytest -q`: 1154 passed, 1 skipped.
- Ad hoc `python -c` scripts for schema, regex, ranking invariants, belief cell recomputation, and catalogue lookups against data/canonical/products.parquet and suppliers.parquet.

## Checklist

| # | Check | Result |
|---|---|---|
| 1 | Schema | PASS: 2/2 lines validate as GTConversation. |
| 2 | Barrier | PASS WITH LIMITS. Cache files hold only {prompt_name, cache_key, response}, so the request payload is not auditable post hoc. All 17 negotiator-turn cache entries were scanned for hidden-field strings. The only hit is `persona_seed` inside cache_key (the seed, not a persona parameter). Responses contain no GEM id, golden_param or bid fields. The barrier tests passed in pytest. Recommend logging the (redacted) payload hash or a barrier digest. |
| 3 | Commercial framing | PASS: 0 hits for GeM, bid, tender, EMD, ministry, MSE, MII, L1, corrigendum or Government across 16 utterances. |
| 4 | Policy | VACUOUS: no PROPOSE turn, so there are no buyer decisions to recompute. `accepted_relaxations` is empty in both conversations; no YES, COUNTER or BYPASS occurred. |
| 5 | State consistency | PASS (trivial): truth state is unchanged, and reference_ranking equals reference_ranking_initial in both conversations. |
| 6 | Unsupported claims | FAIL: see section A. |
| 7 | Ranking | PASS: in all three matrices of both conversations, rows are score-sorted, and every row with hard_fail or hard_unknown > 0 has p_feasible = 0 and p_recommend = 0. I did not recompute weights or match_original independently (SKIPPED). |
| 8 | Randomness | n = 2, only a sanity check: tau 0.942 (band 0.76-0.99) and 0.185 (band 0.05-0.24); both inside their strata. |
| 9 | Reproducibility | SKIPPED: a re-run needs the live or mock pipeline, which I am not allowed to invoke here. |
| 10 | Coverage | 488257: 60 candidates, 3 negotiated product constraints plus 4 commercial terms, 50 verification_only, 4 dropped, initial_feasible 0, relaxations proposed 0, BEST_AVAILABLE. 341930: 135 candidates, 0 negotiated product constraints plus 5 commercial terms, 21 verification_only, initial_feasible 0, relaxations proposed 0, BEST_AVAILABLE. BEST_AVAILABLE share is 100%. |
| 11 | Persona diversity (n = 2) | PASS: min = mean = 0.553; no duplicate names or styles; strata hold. The industry_city check shows a top share of 0.50 (limit 0.34), which is expected at n = 2. |
| 12 | Realism | PARTIAL FAIL: see section D. |
| 13 | D053-D056, D065, D066 | See sections B to F. D056 rows exist for every negotiator turn (4 per conversation, turns 1, 3, 5, 7) but are empty (front_points []). |

## A. Final turns (D069 a): LLM-generated, but with unsupported claims

Generation:
- turn 7 llm_call_ids include negotiator_final (conversation 1 has two calls, so the first draft was probably rejected and retried; conversation 2 has one).
- The wording is not a template.

I checked every claim against the catalogue and compliance_detail_truth.

488257, turn 7:
- Correct: Travel Trek, Saba Fashion, ₹325/Piece, budget PASS (162,500), state Maharashtra PASS. Indian Bags ₹335/Unit and M.J Bags ₹260/Piece are in the catalogue and meet the budget.
- Omission: it says nothing about Saba's rating of 3.8 against the required 4.69 or its tenure of 3 years against 10.4. Both are FAIL in truth and both are listed in unmet_constraints. Option 1 also has FAIL type = Backpack.
- Vague: "fall short on material and supplier details" for options 2 and 3 hides FAIL on colour (Black / ALL), state (Telangana / Delhi), rating and tenure.

341930, turn 7 (clear defects):
- Dharam: "would require compromises on the supplier rating". In truth the rating is 4.6, a PASS against 4.46. The unmet list is only budget, qty_vs_moq and state.
- Dharam: "size" is claimed as a compromise, but the product name is "Cast Iron Double Flange Sluice Valve 200Mm". This is contradicted by the catalogue.
- Dharam: "order quantity" and "material rating" are UNKNOWN in the data (moq is NaN), yet are stated as misses.
- Namokar: "misses the pressure, size, and material" are UNKNOWN (no evidence), not misses. "Misses rating and state" is true (4.3 and Delhi).
- Bluebell: "lacks the pressure", but the product is titled "1079B Cast Iron Sluice Valve PN 1.6 (Flanged)". This is contradicted by the catalogue.
- The code validator checks names and numbers only. It does not check the met / unmet semantics, and the facts handed to the LLM apparently equate UNKNOWN with unmet.
- Ordering: the options are presented in product_id order, not rank order. Option 1 (match 0.4, budget 431,200 against 270,562) precedes two options with match 0.6. The reference top-1 is 22578945888, which is listed second.

## B. Zero-PASS demotion (D069 b)

- 488257: only water_resistance (0 PASS, role_reason zero_pass) was demoted. 49 other constraints were already low_coverage. Negotiated product constraints are main_outside_material, strap_tape_webbing_material and colour (all HARD), plus the 4 synthetic commercial terms.
- 341930: material_of_body, bonnet_material_standard and coating were demoted. Negotiated product constraints = 0. The scenario is a commercial-only negotiation, while the opening voices technical terms that are never scored (for example PN 1.6, non-negotiable).
- initial_feasible = 0 in both, so demotion did not make the scenarios satisfiable:
  - 488257: count_before = 0 even before tightening (tightness steps 9). The best reference candidate (2849554070188, leather, brown) fails budget, rating and state, so only SOFT relaxation could make anything feasible.
  - The strict persona has max_relaxations = 0, so by construction no relaxation can ever be accepted.
  - 341930: the tightening loop ran 10 steps from 14 to 0 feasible. `target_reached: true` with count_after 0 against target 1 relies on the [0, 2] range.
- 341930 qa flag `uncalibrated` is correct (lenient band 0.9-1.0, chosen 0.8, and only 1 candidate step tried).
- 488257 calibration claims in_band at 0.7 for the strict tier. The live result was 0.2, so the calibration simulation does not predict the live negotiator. It uses clean beliefs, while the live belief is misaligned (section C).

## C. Why both ended BEST_AVAILABLE with no relaxation proposed (D069 b)

relaxation_analysis (4 rows each): CLARIFY at turns 1, 3 and 5, BEST_AVAILABLE at turn 7. Every row has front_points [] and empty bounds. The menu was empty at every iteration. `clarify_max_rounds = 3` consumed the turn budget, but the menu was already empty, so clarifying delayed nothing that could have helped.

Root causes, from recomputing the belief cells (compliance_detail_belief):

1. Believed-HARD terms that the catalogue cannot evidence are UNKNOWN for every candidate. These include quantity (60/60 and 135/135 UNKNOWN, matched to nothing or to verification_only total_quantity), strap colour, rain cover, bag style, capacity, top handle, media, end connection and design standard. Result: every candidate has hard_unknown > 0, so p_feasible = 0 for all of them. There are no near-misses and the relaxation search finds nothing. Quantity is an order parameter and should not be a HARD catalogue constraint.
2. Misaligned beliefs in 488257:
   - strap, tape and webbing material map to constraint e137ffde2caf (inside_material). The correct constraint is 4385464b83ce.
   - `color` / `strap_color` has matched_constraint_id None, so the HARD colour constraint is lost.
   - inside_zippered_compartments maps to compartments_inside (a7cccf...), not zipper_compartments_inside.
   - The belief extraction also flip-flops between turns 3 and 5: strap/tape/webbing is merged at turn 3, split at turn 5, and "color" becomes "strap_color".
3. 341930: belief B3 supplier_rating (SOFT, 4.46) is evaluated as UNKNOWN for all 135 candidates, with evidence such as `material=Ductile Iron`. It is evaluated against the wrong catalogue attribute. The truth side evaluates the same rating correctly (PASS or FAIL).
4. The negotiator ranking is degenerate. All p_feasible = 0, so ranks follow product_id order (10968626997, 12817111948, ...). The reference top-1 (2849554070188, match 0.7) sits at belief rank 26.
5. The BEST_AVAILABLE items in 488257 (match 0.2 / 0.1 / 0.1) are not the reference top candidates (0.7 / 0.7 / 0.6 exist). top1_match_original 0.2 is therefore a negotiator-quality failure, not an unavoidable one. In 341930 the choice is 0.4 / 0.6 / 0.6 against a reference top of 0.6.

## D. Realism (D065) and hand reading

Automated:
- No [HARD] / [SOFT] / [NEW] tags.
- No bursts of more than 2 answers per buyer turn.
- No spoken ranking arithmetic.
- The turn_repeat check passes, and the sales_persona check passes (names Rohan and Arjun differ).
- Both sales personas have the same sales_approach string, and the name never appears in an utterance.

Float formatting (still not fixed, D065): "500.0 units", "215335.9375 INR", "40.0 units", "43.38 units" (fractional units), "10.42 years". The two openings differ in format: 341930 uses "INR 2,70,562", 488257 uses raw floats.

Opening flags:
- 488257 has no greeting ("Aavishkar Edutech Solutions requires ...", written in the third person, "Submit specifications immediately").
- 341930 has 6 sentences (limit 5).

Clarify behaviour (c):
- Buyer answers reveal at most 2 constraints per answer (2, 2, 2 and 2, 1, 2). This is PASS.
- Redundancy: 488257 turn 6, "exactly 2 inside compartments, and it must have 2 inside zippered compartments", is the same fact twice. It comes from two separate constraints, one of which was mis-mapped.
- Negotiator questions:
  - 488257 asks "laptop size" and "preferred brand" (turn 1), then "bag style" and "capacity" (turn 3), then "how many compartments" and "preferred brand" again (turn 5). The brand question is repeated and the questions are generic. Some do hit unrevealed constraints (capacity, compartments).
  - 341930 asks valve size / body material (turn 1), end connection / media (turn 3) and brand / design standard (turn 5). These are better targeted at real unrevealed constraints, except "brand".
- The phrasing is formulaic: each negotiator turn is "I have noted X. To narrow this down, ... Also, ...".
- The buyer says "No problem, we'll clarify that for you" (341930 turn 2), which is boilerplate.

Register versus tier: the strict buyer (488257) is terse ("Laptop size is standard. ..."), which fits. The lenient buyer (341930) is warm and semi-formal. This is PASS.

Hand-read, 488257 (strict, 8 turns): Mostly mechanical slot-filling. The buyer opens with a terse, bossy list and answers each question with two spec facts. The negotiator asks two generic questions per turn and cannot act on the answers; none of its belief changes affect what it does. It never tests the leather, brown and budget tension that makes the scenario hard. The close is fluent but gives the buyer weak options (a Backpack with a 3.8 rating) without saying the rating and tenure miss, then offers to check unrelated details.

Hand-read, 341930 (lenient, 8 turns): Reads more like a real exchange. The negotiator's questions target real unrevealed constraints (valve size, end connection, design standard), and the buyer answers in character ("non-negotiable for our project"). The final turn is long, and its claims partly contradict the catalogue (section A). The buyer's "hand wheel standard to be 200 of IS: 210" is a garbled source value passed through verbatim.

## E. best_available_non_compliant (D069 e)

- 488257: 23010963797 and 25861127888 have non_compliant true. Both have a negotiated HARD FAIL on colour (Black / ALL), so the flag is correct. Item 1 (2859010927655) has non_compliant false. Its negotiated HARD cells are UNKNOWN, not FAIL. The FAIL there is `type=Backpack`, which is verification_only, so the flag is consistent with the rule, but it is a FAIL that still counts under D048 and is not surfaced.
- 341930: all false; no negotiated product constraint can FAIL, so this is consistent.
- The qa_flags list the two non-compliant products in 488257.
- hard_violation_rate_recommended = 0.333 in the eval output. This is a mix across the 6 listed items, which is acceptable as reported.

## F. D066 and skipped.jsonl

- skipped.jsonl is absent, and the run reports 0 skipped. This is acceptable because the writer only creates the file when scenarios are skipped (cli.py:229-233).
- Per-item `path=rule|llm` is NOT persisted: no "path" or "item_paths" field in conversations.jsonl or .cache/llm/provenance.jsonl (0 matches). It exists in `LLMResult.paths` and `extra["item_paths"]`, but the runners log it with the logger only (runners.py:415). The only trace is `alignment_llm_call_ids: ["rule:align", ...]` and `claims.n_prefiltered`. FAIL against "each prep item logged path=rule|llm".
- HTTP counter: .cache/llm/pilot_http_count.json shows `total: 39`, which does not match the 101 requests in the task brief. It is likely stale or per-process. provenance.llm_calls live counts are 27 + 57 = 84. Not reconciled.

## G. Other D053-D056 / D059

- The tolerance in levels, the seeded buyer draws and COUNTER rules cannot be tested (no proposal).
- D054 (every utterance generated live): PASS for all 16 turns (each has llm_call_ids with the expected prompt names).
- D059: item_responses are empty everywhere.
- D056 rows are present but contain no information.

## Defects ranked by severity

1. **No relaxation is ever reached (empty menu).** Believed-HARD terms that the catalogue cannot evidence make every candidate hard_unknown, so no near-miss exists. Quantity and other non-catalogue terms should be treated as "to confirm", not as HARD catalogue constraints, or UNKNOWN should not block the near-miss search. Owner: matching-engineer (relaxation and ranking), with dialogue-engineer (belief construction).
2. **Belief misalignment.** Examples: 488257 strap, tape and webbing to inside_material; colour unmatched; zipper compartments to compartments_inside; 341930 supplier_rating evaluated against material. Owner: dialogue-engineer (alignment and matching of belief to constraints) and llm-engineer (negotiator_extract v2 attribute naming).
3. **Unsupported "unmet" claims in the final turn.** Dharam rating and size, Bluebell pressure, and UNKNOWN stated as a miss. The validator does not check met / unmet semantics, and the facts payload equates UNKNOWN with unmet. Owner: dialogue-engineer (facts builder and validator) and llm-engineer (negotiator_final prompt).
4. **Wrong option selection and order for BEST_AVAILABLE.** The reference top candidates (0.7) are ignored in favour of 0.2 / 0.1 matches (488257). Options are listed in product_id order rather than match order (341930). The calibration simulation predicts 0.7 to 0.9 while the live run gets 0.2. Owner: dialogue-engineer (selection) and matching-engineer (calibration should simulate the real belief path).
5. **Scenarios unsatisfiable by construction.** initial_feasible = 0 in both; the strict persona has max_relaxations = 0; 341930 has no negotiated product constraints. Owner: data-steward (scenario construction) with the coordinator for the max_relaxations strata.
6. **D066 path=rule|llm not persisted.** Owner: llm-engineer.
7. **D065 float formatting and opening checks.** "500.0 units", "215335.9375 INR", "43.38 units", missing greeting, 6-sentence opening. Owner: dialogue-engineer for the opening and number formatting; llm-engineer for the buyer_opening prompt.
8. **Repeated generic brand question and redundant reveal.** Owner: dialogue-engineer and llm-engineer (negotiator_clarify prompt).
9. **Possible false FAIL.** Bluebell "Flanged" vs end_connection_type = Flange (verification_only, still scored). I did not inspect the normalisation. Owner: matching-engineer (value comparison) or data-steward.
10. **Barrier auditability.** Payloads are not logged. Owner: llm-engineer.
11. **HTTP counter mismatch** (39 against 101). Owner: llm-engineer.
