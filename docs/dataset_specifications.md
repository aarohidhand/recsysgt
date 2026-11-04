# Dataset Features — GT Conversation Specification

Answer to Your Questions
1. Dataset Features Stored (Complete Field Inventory)
Storage format: out/gt/<run_id>/conversations.jsonl — one JSON line per (bid, persona). 12 pilot runs exist.
A. Conversation-level Identity & Metadata
Field	Type	Notes
scenario_id	str	{bid}_s{persona_seed}
run_id	str	pilot_<timestamp>_<hex>
bid_ref	str	full GeM bid path (hidden)
provenance	dict[15]	model, provider, timestamps, counts, cache paths, settings hash, prompt versions, llm_model, llm_backend, persona_llm_call_id, profile_llm_call_id, alignment_llm_call_ids, n_candidates, llm_calls, relaxation
qa_flags	list[str]	per-conversation QA issues
top1_match_original	float	final agreed-state match score of top-1 rec
top1_perfect_deal	bool	top-1 satisfies all negotiated constraints in final truth
B. Personas Created (Buyer Side — NOT negotiator)
Component	Fields
buyer_profile	company_name, industry, city, style, llm_call_id
persona	persona_seed, tau (hardness 0–1), tau_band, tier ∈ {lenient, medium, medium_strict, strict}, category_tolerance (7 categories), counter_band, patience_turns, max_concessions_per_turn, ranking_weights (7 normalized features), commercial_terms, max_relaxations, patience_rounds, decision_randomness, tolerance, llm_call_id
C. Constraints USED for the Conversation (Buyer Truth State)
Component	Fields
commercial_brief	scenario_id, bid_ref, quantity, quantity_unit, budget_inr, constraints[], scenario_meta
each BuyerConstraint	constraint_id, bid_id, golden_param (HARD/SOFT determinant), hardness, provenance (gem/synthetic_commercial), scenario_role (negotiated/verification_only/dropped), evidence_coverage, merged_from, attribute, spec_name, buyer_value, operator, unit, allowed_values, allowed_values_raw, allows_higher, constraint_category, constraint_type, scope, condition, is_doc_proof, doc_name, framing_decision (keep/translate/drop/merge), framing_reason, commercial_text, raw_text, evidence_anchor, llm_call_ids
turn[0].buyer_truth_state_after	Full 32-constraint set after opening
D. Turn-wise Columns (per turns[] entry)
Field	Type / Notes
turn_idx	int
speaker	BUYER / NEGOTIATOR
action	OPEN_REQUEST / CLARIFY / PROPOSE_RELAXATION / RECOMMEND / BEST_AVAILABLE / YES / NO / COUNTER / BYPASS / MIXED / REVEAL / ACKNOWLEDGE
utterance	str (natural language)
proposal	Proposal (option/bundle) when action=PROPOSE_RELAXATION
proposal_outcome	ACCEPTED / REJECTED / COUNTERED / BYPASSED / PARTIAL
counter_value	str when COUNTER
item_responses[]	{constraint_ref, action, counter_value} per bundle item
proof_requests[]	D055: {turn_idx, product_id, constraint_id, attribute, doc_name}
revealed_constraint_ids[]	constraints voiced this turn (progressive reveal D044)
buyer_truth_state_after[]	buyer's constraint set after this turn
negotiator_belief_state[]	BelievedConstraint: belief_id, attribute, operator, value, unit, believed_hardness, is_doc_proof, source_turn, matched_constraint_id
feasible_count_truth	int — candidates meeting truth-state constraints
feasible_count_belief	int — from negotiator belief state
llm_call_ids[]	list of LLM call IDs in this turn
E. Final Ranking Matrix Columns (27 per candidate, in both reference_ranking & negotiator_ranking)
product_id, supplier_id,
hard_pass, hard_fail, hard_unknown, soft_pass, soft_fail, soft_unknown,
pending_verification_docs[],
spec_match, price_score, price_vs_budget, moq_fit, rating_bayes, rating_count,
tenure_years, location_match, evidence_confidence,
p_feasible, utility, score, p_recommend,
rank, eligible,
match_original,
verification_required[]
- p_feasible = Π p_c (PASS→evidence_confidence×0.9 if not raw_text_verified; PENDING→same as PASS; SOFT UNKNOWN→0.5; SOFT FAIL→0.3; HARD UNKNOWN/FAIL→0)
- utility = Σ w_i·feature_i (weights from persona)
- score = p_feasible·utility; p_recommend = softmax(score/T); rank = Plackett-Luce expected order
- match_original = weighted share of ORIGINAL negotiated constraints (HARD=2, SOFT=1, verification_only excluded)
F. Final Outcome Detail
Field	Notes
outcome	RECOMMENDED / BEST_AVAILABLE
best_available	only if BEST_AVAILABLE: items[] with product_id, supplier_id, match_original, unmet_constraints[], verification_required[], non_compliant
best_available_reason	no_feasible / patience_exhausted
reference_ranking_initial[]	truth ranking before any concession (D052)
accepted_relaxations[]	accepted RelaxationOptions
pending_verification_docs[]	aggregated doc list
proof_requests[]	aggregated D055
compliance_detail_truth[] / compliance_detail_belief[]	full ComplianceCell audit: product_id, constraint_ref, status∈{PASS,FAIL,UNKNOWN,PENDING_VERIFICATION}, evidence, evidence_confidence, pending_doc, scored
relaxation_analysis[]	D056 per-negotiator-turn relaxation iteration
G. State-wise Features (for downstream analysis)
- By tier: persona.tier ∈ 4 buckets with per-tier match_original bands in settings (recommendation.tier_targets)
- By constraint source: provenance ∈ {gem, synthetic_commercial}; by scenario_role ∈ {negotiated, verification_only, dropped}
- By compliance: four-valued status ∈ {PASS, FAIL, UNKNOWN, PENDING_VERIFICATION}

2. Where Test Conversations Are Stored
- All existing conversation JSONL files (12 pilot runs): out/gt/pilot_*/conversations.jsonl
- No separate test conversation files exist elsewhere.
- tests/fixtures/llm/ holds LLM mock fixtures (prompts/responses), not saved conversations.
- To generate: python -m gtgen.cli pilot --mock --bid <id> --personas 1 (mock, fast); --live (real LLM via OpenRouter).
  Live batches run one bid per process via scripts/live_all_bids.sh (D084); --personas is 1 per bid (D080).
