---
name: persona
version: 2
purpose: Invent a fictional Indian private company and its negotiation personality (tau chosen by the model) and pick its commercial terms inside code-supplied ranges.
input_vars: [product_category, quantity, quantity_unit, budget_inr, categories_json, weight_keys_json, nonce, tau_guidance, bands_json, tightness_hint]
output_schema: PersonaOutputV2
temperature_key: persona
---
Invent ONE fictional Indian private company that wants to buy "{{product_category}}" (quantity {{quantity}} {{quantity_unit}}, budget in INR: {{budget_inr}}) and describe how it negotiates. Output JSON only.

Scenario nonce: {{nonce}}. Treat the nonce as a random seed: different nonces must give clearly different companies, cities, speaking styles and numbers. Do not reuse common names or the typical "Hydro/Vardhan/Vajra" style names.

Fields:
- company_name: invented, plausible Indian private company name.
- industry: an industry that would genuinely buy this product (consistent with the product).
- city: an Indian city.
- style: 1-2 sentences describing how the buyer speaks (for example formal and terse, chatty and warm, impatient, detail-obsessed).
- tau: how strict the buyer is, from 0 (very flexible) to 1 (very strict). Pick a value that is not 0.5, 0.7 or 0.3 and use 3 decimals. Use the whole range 0.050 to 0.950.
- category_tolerance: one entry for EACH of these requirement categories: {{categories_json}}. multiplier from 0.0 to 1.5: how much the buyer will flex on that category (0 = never flex, 1 = average, 1.5 = very relaxed).
- counter_band: 0.00 to 0.40, how far past its limit the buyer will still counter-offer.
- patience_turns: integer 3 to 12, how many exchanges the buyer tolerates.
- max_concessions_per_turn: integer 1 to 4.
- ranking_weights: non-negative importance for each of {{weight_keys_json}} (they need not sum to 1).
- commercial_terms: the company's own purchasing terms. The allowed ranges are in this JSON (key "terms"):
{{bands_json}}
  For a "numeric" term pick one number between lo and hi (inclusive). For a "set" term pick a list of between min_k and max_k values copied exactly from options. "tight_end" tells you which end is the more demanding one: "lo" means a lower number is more demanding, "hi" means a higher number is more demanding, "fewer" means a shorter list is more demanding. Give exactly the terms listed under "terms" and no others (terms under "omitted" must not appear). Meaning of each term: budget_inr = the most the company will pay in total, in INR; qty_vs_moq = how far above the required quantity a supplier's minimum order quantity may be, as a fraction of the quantity (0.25 = up to 25 percent above); min_supplier_rating = the lowest supplier star rating it accepts; min_tenure_years = the fewest years a supplier must have been trading; preferred_states = the Indian states it prefers suppliers from. Pick values that fit the personality and the product.
  {{tightness_hint}}

{{tau_guidance}}

Tau, tolerance, weights and commercial terms should fit the personality you describe. Return one JSON object with exactly these keys (commercial_terms holds only the terms allowed above):
{"company_name":"","industry":"","city":"","style":"","tau":0.0,"category_tolerance":[{"category":"","multiplier":0.0}],"counter_band":0.0,"patience_turns":0,"max_concessions_per_turn":0,"ranking_weights":{"spec_match":0.0,"price_score":0.0,"moq_fit":0.0,"rating_bayes":0.0,"tenure_years":0.0,"location_match":0.0,"evidence_confidence":0.0},"commercial_terms":{"budget_inr":0.0,"qty_vs_moq":0.0,"min_supplier_rating":0.0,"min_tenure_years":0.0,"preferred_states":[""]}}
