# Audit: live stage-1 pilot pilot_20261031_201309_b2ee3452 (2 conversations, bids 488257 and 341930, 1 persona each)

Auditor: gt-auditor. Offline only (no network, no LLM calls). Read-only on code/data.
Commands run: `python -m gtgen.cli eval --run pilot_20261031_201309_b2ee3452`; python -c/heredoc analysis scripts over
out/gt/pilot_20261031_201309_b2ee3452/conversations.jsonl, .cache/llm/*.json, .cache/llm/provenance.jsonl; grep over src/, prompts/.

## VERDICT: FAIL
The run is mechanically valid (schema, framing regex, no hard relaxation) but it is a degenerate conversation: 1 buyer turn + 1
negotiator turn, the negotiator turn is deterministic template text (D054 violation), no clarify/relaxation round, and the
reference ranking carries no signal (all scores 0).

## Checklist
| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Schema | PASS | 2/2 lines validate as GTConversation |
| 2 | Barrier | PASS WITH ISSUES (partial) | cache stores responses + context only, NOT request payloads, so payload leak cannot be proven absent. 0 hits for GEM/, golden_param, gem_bid_id, search_keyword, relevance_score in the 2 live negotiator_extract cache files. prompt negotiator_extract.v1 takes only transcript_json. Issue: negotiator-call logged context holds persona_seed=222957484967780 (the real derived scenario seed, == provenance only as "persona_seed" log), while the negotiator_seed is 1157922638 / 1736172755 (D061 intent). Log metadata only, not payload; but .cache is readable by anything. |
| 3 | Commercial framing | PASS WITH ISSUES | regexp (GeM, bid, tender, bidder, EMD, ePBG, ministry, department, MSE, MII, Make in India, L1, corrigendum, Government): 0 hits in 4 utterances. Only "IS:" x2 and "Clause" x1, GEM_2019_B_341930_s1 turn 0 (see ruling below). |
| 4 | Policy | PASS (vacuous) | 0 proposals, 0 buyer decisions, 0 item_responses, 0 relaxations in either conversation. Nothing to check; hard constraints stayed untouched (accepted_relaxations=[]). |
| 5 | State consistency | PASS (vacuous) | truth state unchanged (no change accepted); but see unsupported/incorrect utterance claims in item 6. |
| 6 | Unsupported claims | FAIL | see below |
| 7 | Ranking | FAIL (usability) | rows sorted by score and ranks consistent, no HARD FAIL/UNKNOWN row has p_recommend>0 (0 rows with p>0). BUT every row of reference_ranking, reference_ranking_initial and negotiator_ranking has score 0 and p_recommend 0 (1 distinct score in 60 and 135 rows; sum p = 0): the rank order is an arbitrary tie order. NDCG/MRR/MAP/P@k all 0.0. Reference top-1 (10968626997 / 18461463997) is not among the offered products. |
| 8 | Randomness | INCONCLUSIVE | n=2: tau 0.682 (medium_strict, band [0.51,0.74]) and 0.114 (lenient, [0.05,0.24]); mean 0.398. tau_spread_check (min_std 0.18, min_tiers 3) cannot be evaluated with 2 points (2 tiers only). Both values inside their stored strata. |
| 9 | Reproducibility | SKIPPED | needs a pipeline re-run (--live or mock-with-warm-cache); forbidden in this audit |
| 10 | Coverage | reported | bid 488257: 60 candidates, truth cells 3420 (UNKNOWN 84.7%, FAIL 382, PASS 140); 61 constraints = 8 negotiated (4 GEM + 4 synthetic commercial) / 49 verification_only / 4 dropped (role_counts negotiated 4 is GEM-only); bid 341930: 135 candidates, 3510 cells (UNKNOWN 74.0%, PENDING 24), 27 constraints. Relaxations proposed 0/0, accepted 0/0. BEST_AVAILABLE share 2/2 = 100% (reason no_feasible). 0 of 60 / 0 of 135 candidates satisfy every negotiated HARD constraint, so zero feasible scenario by construction (initial_feasible 0). |
| 11 | Persona diversity | PASS (weak) | see below |
| 12 | Realism | FAIL | see below |
| 13 | D053/D054/D055/D056/D059/D065/D066 | FAIL | see below |

## Item 13 / D054: the negotiator turn-1 text is TEMPLATE text, not LLM output  (CRITICAL)
* Code: src/gtgen/dialogue/_best.py:75-76 literally contains "I am sorry, but I could not find any product that meets all of your stated
  requirements, and I have no further alternatives to suggest. These are the closest options." and :81 " Option {n}: {label}; it does not meet,
  or I cannot confirm, your requirement on ...". negotiator.py:21 docstring: "The deterministic RECOMMEND / BEST_AVAILABLE utterances are built only
  from catalogue fields"; negotiator.py:386-388 returns `utterance=best_available_text(...)`. No LLM call is made for it.
* Data: turn 1 llm_call_ids in the GT = negotiator_extract + doc_claim (conv 488257: 02639bf4 extract, ebac744e/6f854cea doc_claim; conv 341930:
  a00967bd extract). Looked up in .cache/llm/provenance.jsonl: no negotiator_reply, negotiator_clarify or negotiator_interpret call exists for either
  scenario in this run (actual calls per scenario in .cache/llm/provenance.jsonl: persona, buyer_opening, reveal_check, negotiator_extract, doc_claim only; note the conversation-level provenance.llm_calls.by_call dict is really the prompt-version map and misleadingly lists negotiator_reply/clarify).
* Therefore the same sentence appears verbatim in both conversations (conv-independent), and any BEST_AVAILABLE or RECOMMEND turn anywhere is template text.
  Contradicts D054.1 ("template fallbacks forbidden", every LLM step live) and D054.3/4. D050 text design in _best.py was never re-based on D054.
  scenario_meta has no `sales_persona` (D054.3: generated once per scenario and stored): realism sales_persona check = SKIP, nobody generated it.
* Also the claim `live_share 1.0` in provenance (14 and 44 calls) is true for the LLM calls that exist; it does not show that every utterance is LLM.
* Owner: dialogue-engineer (negotiator final turns -> LLM with allowed facts) with llm-engineer (prompt); coordinator to amend D050/D054 (the rule and the design disagree).

## Why the negotiator went straight to BEST_AVAILABLE at turn 1 (no clarify, no relaxation)
1. negotiator.py:751-752: `_should_clarify` (D044) is evaluated ONLY inside the `if len(feasible) >= self._min_shortlist` branch. With
   feasible=0 (both conversations: feasible_count_belief 0) clarification is skipped entirely.
2. Then `build_menu` is built from believed SOFT negotiable rules only. Conv 488257: the SOFT beliefs are supplier_rating and trading_history;
   all 60 candidates also carry HARD UNKNOWN/FAIL on beliefs; the front is empty (relaxation_analysis[0].front_points=[]) ->
   negotiator.py ~765 "if menu.diagnostic or not menu.points: ... _best_available_step" -> BEST_AVAILABLE with reason no_feasible.
   relaxation_analysis has exactly 1 row (turn 1, chosen_action BEST_AVAILABLE, no bounds).
3. The scenario itself is infeasible by construction: tightness count_before=0, initial_feasible=0 in both; calibration shows 11/11 candidates with
   stop_reason no_options, n_rounds 0. 488257: only 9/60 candidates PASS leather outside material, 10/60 PASS brown, none passes all HARD negotiated;
   341930: material_of_body PASS 0/135 (UNKNOWN 134, FAIL 1), bonnet standard PASS 0/135, coating PASS 0/135. HARD cannot be relaxed (rule 6) so no
   relaxation exists. Both scenarios kept qa_flag `uncalibrated` (chosen top1 0.667 vs band [0.75,0.85]; 0.4545 vs [0.9,1.0]).
   The pipeline should have rejected/reshaped such scenarios in calibration, or the clarify round should have run so the other 53/19 constraints
   were ever voiced. Voicing: 488257 61 constraints, opening voiced 8 (6 negotiated/verification + rating + tenure); budget (320000 INR) and
   preferred_states were never voiced yet appear as unmet in BEST_AVAILABLE (truth unmet list). 49 verification_only items were never discussed.
   Owners: dialogue-engineer (clarify gate), matching-engineer / data-steward (calibration: refuse zero-feasible scenarios; do not mark uncalibrated scenarios as GT).

## Item 6: unsupported / incorrect negotiator claims (4 utterances sampled = all negotiator turns)
Facts that check out: product names, supplier names, prices (₹1,750x500=875000 -> price_vs_budget 2.734; ₹450 -> 0.703; conv2 5000/3000/4830 x40 = 200000/120000/193200 match).
Failures:
* GEM_2019_B_341930_s1 turn 1: all three options "do not meet ... budget". Budget 345000 INR; offered totals are 200000, 120000, 193200 (belief cells FAIL
  evidence budget=200000.0 etc.). Cause: negotiator_extract returned budget operator "=" 345000 (cache a00967bd) so 200000 != 345000 -> FAIL. False claim.
* GEM_2019_B_341930_s1 turn 1: option 2 (tenure 17 y), option 3 (12 y), option 1 (9 y) listed as not meeting "trading history" (needs 7.5 y). Hedged by "or I cannot confirm"
  but the catalogue contradicts it. Same in GEM_2020_B_488257_s1 option 2/3 (tenure 15 y vs needed 9.5 y).
* GEM_2020_B_488257_s1 turn 1: all three options fail "product type" while they are "Leather Laptop Bag"/"Pu Leather Office Laptop Bag" for the requested "Laptop / Office Bag".
  Belief B1 product_type HARD "=" -> UNKNOWN for every candidate. Also the identical unmet list ("product type, supplier rating, trading history") for all three options is a
  blanket artefact, not a per-product statement; GT final.best_available.unmet_constraints (water_resistance, budget_inr, min_supplier_rating...) differs from what is said.
* GEM_2020_B_488257_s1 turn 1 offers "Pu Leather Office Laptop Bag" (Shree Narmada) for a "must be leather" buyer without saying so (the hard leather cell is UNKNOWN/PASS from the listing; PU leather is not leather).
* Owner: dialogue-engineer (utterance from verified facts), llm-engineer (negotiator_extract: operator for budgets, quantity/product type are not specs; prompt v1 is the only
  non-v3 negotiator prompt, it was not touched by D065).

## Item 3 / "IS:" and "Clause" ruling
CLAUDE.md §1 rule 4 bans GeM, bid numbers, ministries, EMD/ePBG, MSE/MII preferences, tender language. IS (Bureau of Indian Standards) standards are national
technical standards used by private manufacturers; citing "IS 14846" is not a government-procurement term. Ruling: not a rule-4 violation. However, the verbatim
raw-spec phrasing is tender boilerplate: "Bonnet material must be as per 200 of IS: 210 - latest", "as per Clause 9.1 and 9.2 of IS: 14846" (GEM_2019_B_341930_s1 turn 0)
is unnatural for a lenient, informal buyer and "- latest" is a tender artefact. WARN; recommend the reframing step rewrites such clauses ("coating per IS 14846") (llm-engineer reframe prompt).
Also: raw "Gr-FG", "200 of IS: 210 - latest", "500.0 units", "40.0 units", "345000.0 INR" are not natural. Float formatting is a code bug, see below.

## Item 11: persona diversity (n=2)
`gtgen.eval.persona_diversity`: pairwise distance 0.651 (min = mean = median, 1 pair), threshold 0.15 -> PASS. company names distinct (NexGen Edutech Solutions, Kalyani Agro-Flow Systems), styles distinct,
no near-duplicate names, strata contain all values (checked by hand too: tau, patience_rounds, max_relaxations, decision_randomness, tolerance per category all inside the stored band).
Inconsistency: industry_city check prints "top pair ... share 0.50 (max 0.34)" yet PASS (the two scenarios have different industry/city; the 0.50 is the 1/2 share of any single pair), logic should not flag/print n=2.
Can conclude: the stratifier produced separated traits (0 vs 1 strata on all dimensions) and the LLM stayed inside its strata. Cannot conclude: anything about mode collapse, tau spread (std needs >=3 tiers), repeated
(industry, city) rate, the diversity of company names beyond 2, or whether the distance threshold is meaningful. One stratum per trait per scenario means n=2 is the maximum-separated case.
Note: Persona keeps legacy fields (patience_turns 5/10, max_concessions_per_turn 1/3, category_tolerance with OTHER/COMMERCIAL) next to the D053 fields (patience_rounds 1/4, tolerance, max_relaxations 2/1): two
patience counts disagree (5 vs 1, 10 vs 4); owner contracts (coordinator). Conv 341930: tier lenient but the voice says "no compromise there" on coating, mixed with an 8-sentence ramble.

## Item 12: realism (D061/D065); fewer than 5 real samples: 2 conversations, read fully
Deterministic: `gtgen.eval.realism` verdict FAIL: GEM_2020_B_488257_s1 opening_greeting FAIL (no greeting); GEM_2019_B_341930_s1 opening_sentences FAIL (8 sentences, allowed 2..5);
run turn_repeat PASS; sales_persona SKIP (none recorded).
Checks: inline tags [HARD]/[SOFT]/[NEW]: 0 hits. Enumerated burst: opening conv 488257 lists 5 requirements in 3 sentences, conv 341930 lists body, bonnet, coating, rating, tenure, states in one message -> burst-like (8 constraints revealed in turn 0 in BOTH, above progressive_reveal.opening_max 7 and the "2-4 short turns" intent).
Spoken arithmetic by the negotiator: no counts/gain/cost words; but the template enumerates internal attribute names ("product type, supplier rating, trading history", "Still to confirm: quantity, strap material, tape material, technical specification sheets, water resistance, webbing material") = slot-filling.
Buyer register vs tier: conv 488257 medium_strict: formal/terse, no greeting -> ok-ish; conv 341930 lenient: informal, verbose -> ok. Sales persona name/tone: none (no sales_persona; no LLM reply).
Budget/qty vs catalogue: 500 bags at 450-1750 = 225k-875k vs budget 320k (2 of 3 offers are 2.5-2.7x budget; the budget was not voiced); 40 valves at 3000-5000 = 120k-200k vs 345k ok.
Formatting bugs: "500.0 units", "around 40.0 units", "budget of 345000.0 INR" (float repr from the brief leaks to speech, buyer prompt/brief builder; owner dialogue-engineer, plus the brief quantity as float). Also "Laptop / Office Bag" raw product_target phrase.

Hand-read paragraphs (2 conversations only; NOT 5 samples):
1. GEM_2020_B_488257_s1 (Pune, medium_strict, terse/formal). The buyer opens with one dense paragraph stating company name and quantity before any greeting, lists material, strap material, colour, water resistance (inverted: "not required"), rating, tenure, then asks for spec sheets. The negotiator replies with a single canned apology plus three options and a list of leftovers; the buyer never answers. This is slot-filling and a monologue pair, not back-and-forth: there is no exchange, only request then verdict.
2. GEM_2019_B_341930_s1 (Coimbatore, lenient, chatty). The buyer opening is warm and fully in persona ("hope you are doing fine", "happy to sit and talk through the details") but is 8 sentences and reads constraints off the spec sheet ("200 of IS: 210 - latest", "Clause 9.1 and 9.2"). The negotiator returns the same canned apology (word for word as in conversation 1) and the buyer's invitation to talk it through is ignored. Not a real back-and-forth.
3. Buyer persona texture: the buyer LLM output is genuinely persona-flavoured and different across the two scenarios (formal Marathi-English corporate vs informal Tamil-inflected), no regional slang overdone; company names read plausible. Weaknesses: raw floats, spec-sheet phrasing, constraints dump.
4. Negotiator voice: there is none; the only turn is deterministic text with robotic phrasing ("it does not meet, or I cannot confirm, your requirement on ...") and fixed apology; no mention of the buyer's words (D054.4 "reference the previous turn" fails: it does not reference anything the buyer said), no sales persona.
5. Opening shape: both openings reveal 8 of the brief's constraints (conv 1: 8/61 ids but all 8 negotiated-or-high-signal; conv 2: 8/27), not a progressive-reveal subset of 5-7; conv 1 lacks a greeting, conv 2 has one but is too long; neither is a bullet list.

## Item 13 detail
* D053 buyer recompute: SKIPPED/vacuous, 0 buyer decisions to recompute (no proposal). Cannot assert the policy was exercised live. `policy_compliance_rate`, `state_transition_accuracy`, `turns_to_agreement`, `gain_estimate_error` are all null in eval.
* D054: every utterance generated live: FAIL (negotiator turn 1 is template in 2/2; buyer openings are live: buyer_opening llm_call 3 + reveal_check). Exact-duplicate utterances across conversations: the negotiator's first 2 sentences are identical in both (near-verbatim repeat across conversations, undetected by turn_repeat PASS because the options differ).
* D056 rows: 1 RelaxationAnalysisRow per negotiator turn (turn 1) in each conversation: PASS formally, but it is empty (front_points [], chosen_point null).
* D055 proof requests: 0 (proof_requests [] in both); `rate_by_tier` never exercised. Note 341930 pending_verification_docs ['test report','liquid penetrant test report','IS certification','ISI marking certificate'] came from supplier claims (24 PENDING cells), unaffected by ranking: PASS.
* D059 id spaces: no item_responses, SKIPPED.
* D065 tags/arithmetic: tags 0, spoken arithmetic 0, burst: see above.
* D066: prep path logging: NOT persisted. `grep -c '"path"' .cache/llm/provenance.jsonl` = 0; runners.py logs `n_rule/n_llm/n_prefilter/n_cache` only via logger.info (runners.py:335); not in GT provenance or scenario_meta (scenario_meta.claims has n_prefiltered 155 / 477). The per-item rule|llm path therefore cannot be audited from the artifacts. skipped.jsonl: not present in this run directory (no scenario skipped; 0 skipped is consistent with 2 conversations). Prior run pilot_20261031_185427_195fc8e4 has skipped.jsonl (both scenarios skipped at doc_claim, 429/disconnect), format fine.
* qa_flags: `uncalibrated` (2/2) and `reframe_value_reworded:GEM/2019/B/341930_C_db2aae7b38a3` (a verification_only 'Accessories inclusive in the scope of supply' translated to "Accessories included in the supply." - harmless, keep decision recorded).
* Reframe issues seen: 'Water resistance is not required.' is a HARD negotiated constraint with operator '='; it makes 18/60 candidates (the water-resistant ones) FAIL and appears as "unmet" in all offered options (conv 488257): negated requirement modelled as equality to "no". A buyer saying "not required" should not penalise a product that has it. Belief side: B8 water_resistance "required"/"no" HARD.
  'Material is Grey Cast Iron, Gr-FG' (verification_only) duplicates 'Body material ...' (negotiated) in the same opening, conv 341930.
* Hard-constraint handling: no HARD relaxed, no HARD UNKNOWN treated as PASS: BEST_AVAILABLE items 0 with non_compliant=true despite hard_fail 1-2 for the conv-341930 offered products (item 1,2,3 each have hard_fail >=1 in reference_ranking: 1,1,2) -> candidates whose truth row shows hard_fail>=1 (which may include verification_only observed fails, not separated here) are listed with `non_compliant: false`. Check: final.best_available.items[*].non_compliant is False for all 6 items while reference rows show hard_fail>=1 (conv 488257: item 21854387812 hard_fail 1). CLAUDE.md §3.7 says a negotiated HARD FAIL may appear only with non_compliant=true. Possible rule violation (the flag is derived from belief cells where the cells are UNKNOWN, not truth). Owner: matching-engineer (best_available non_compliant on truth vs belief) - needs coordinator ruling since non_compliant is computed belief-side ("current_cells").
* Ordering: BEST_AVAILABLE offer order is not by match_original desc (conv 488257: 0.583, 0.667, 0.583; conv 341930: 0.364, 0.364, 0.4545); stored match_original is truth-side while the order comes from the belief side. Owner: matching-engineer.

## Defects ranked by severity
1. CRITICAL (llm-engineer + dialogue-engineer, decision by coordinator): negotiator RECOMMEND/BEST_AVAILABLE text is a fixed template (_best.py:75-81, negotiator.py:386-388); D054 violated, identical sentences in all conversations; no sales persona stored; no reply to the previous buyer turn.
2. CRITICAL (matching-engineer + data-steward): scenarios with zero feasible candidates by construction (initial_feasible 0, calibration no_options x11, `uncalibrated` 2/2) are emitted as GT; reference ranking scores/p_recommend all 0 (1 distinct score in 60 and 135 rows), so ranking GT carries no information and every ranking metric is 0.
3. HIGH (dialogue-engineer): clarify (D044) is gated on feasible>=min_shortlist (negotiator.py:751), so an infeasible scenario never gets a clarify round; conversation = 1 buyer + 1 negotiator turn, 0 proposals, 0 buyer decisions; D053/D055/D056 untested on live output.
4. HIGH (llm-engineer): negotiator_extract v1 emits budget "=" (not "<="), treats quantity and product_type as HARD product specs; downstream the negotiator wrongly says "does not meet budget" for 200000 vs 345000 (false unsupported claim, conv 341930 all 3 options) and "product type" unmet for a Laptop Bag.
5. HIGH (matching-engineer): best_available `non_compliant=false` while truth rows show negotiated HARD FAIL (6 items); offer order not match_original-desc; belief unmet lists are blanket identical.
6. MEDIUM (llm-engineer reframe/data-steward): negated requirement "Water resistance is not required" modelled as '= no' and fails water-resistant products; duplicate material constraint voiced twice; tender-boilerplate phrasing ("200 of IS: 210 - latest", "Clause 9.1 and 9.2") survives into speech; IS standards themselves are acceptable (ruling above).
7. MEDIUM (dialogue-engineer): opening reveals 8 constraints (opening_max 7); 8 sentences (conv 341930); no greeting (conv 488257); float formatting "500.0 units", "345000.0 INR", "40.0 units".
8. MEDIUM (llm-engineer / coordinator): D066 per-item path=rule|llm not persisted in any artifact; negotiator log context carries the raw derived persona_seed (222957484967780) though the negotiator_seed is separate; request payloads not retained so item 2 cannot be proven from the cache.
9. LOW (matching-engineer): persona_diversity prints industry_city share 0.50 > max 0.34 but PASS; legacy persona fields (patience_turns vs patience_rounds, max_concessions_per_turn) contradict D053 fields (coordinator).
10. Not defects but limits: items 8, 9, 13-recompute inconclusive/skipped (n=2, no network, no buyer decisions).
