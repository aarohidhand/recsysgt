---
name: persona
version: 6
purpose: D072 - the sales_persona (name, speaking_style, sales_approach) must differ from the sales negotiators already used in the batch (listed in avoid_guidance). D070 - like v4 (D060) but no supplier rating / years-of-experience terms (ranking-only), ranking_weights ordered location >= years >= rating. Like v3 but every personality trait must lie inside a code-assigned stratum band and the company must differ from those already created. Invent this scenario's fictional Indian private company (voice traits, tau chosen by the model, relaxation behaviour, commercial terms inside code-supplied ranges) and the negotiator's own sales persona.
input_vars: [product_category, quantity, quantity_unit, budget_inr, categories_json, weight_keys_json, nonce, tau_guidance, bands_json, tightness_hint, ranges_json, strata_guidance, avoid_guidance]
per_conversation_inputs: [product_category, quantity, quantity_unit, budget_inr, categories_json, nonce, tau_guidance, bands_json, tightness_hint, strata_guidance, avoid_guidance]
output_schema: PersonaOutputV3
temperature_key: persona
---
Invent ONE fictional Indian private company that wants to buy "{{product_category}}" (quantity {{quantity}} {{quantity_unit}}, budget in INR: {{budget_inr}}) and describe how it negotiates. Output JSON only.

Scenario nonce: {{nonce}}. Treat the nonce as a random seed: different nonces must give clearly different companies, cities, speaking styles and numbers. Do not reuse common names or the typical "Hydro/Vardhan/Vajra" style names.

Fields:
- company_name: invented, plausible Indian private company name.
- industry: an industry that would genuinely buy this product (consistent with the product).
- city: an Indian city.
- style: 1-2 sentences describing how the buyer speaks (for example formal and terse, chatty and warm, impatient, detail-obsessed).
- tau: how strict the buyer is, from 0 (very flexible) to 1 (very strict). Pick a value that is not 0.5, 0.7 or 0.3 and use 3 decimals, inside the required tau band below.
- category_tolerance: one entry for EACH of these requirement categories: {{categories_json}}. multiplier from 0.0 to 1.5: how much the buyer will flex on that category (0 = never flex, 1 = average, 1.5 = very relaxed).
- counter_band: 0.00 to 0.40, how far past its limit the buyer will still counter-offer.
- patience_turns: integer 3 to 12, how many exchanges the buyer tolerates.
- max_relaxations, patience_rounds, decision_randomness, tolerance: how this buyer concedes. The allowed ranges are in this JSON: {{ranges_json}}. max_relaxations = how many changes to its requirements the buyer will make in total; patience_rounds = how many rejected proposals it tolerates; decision_randomness = how unpredictable its yes/no is (low = consistent, high = moody); tolerance = ONE entry per requirement category in {{categories_json}} (category, value): how far the buyer will let that category slip (0 = not at all). Stay inside the ranges and make them fit the personality. For this company the exact bands below (section Required bands) are narrower than the general ranges and take precedence.
- voice: formality, verbosity and regional_voice words describing how the buyer writes (for example formal / terse / a Marathi-English business register). These shape every message the buyer writes.
- sales_persona: the SALES NEGOTIATOR who will talk to this buyer, a different person on the marketplace side: name (first name only; Indian first names, never Arjun unless nothing else fits, and never one already used in this batch), speaking_style, sales_approach (these three must clearly differ from every sales negotiator already used in the batch, listed below if any; vary tempo, formality and approach, for example consultative, brisk, story-telling, numbers-first) (warm, eager to close, but only ever states facts it can back up). Do not mention the buyer's strictness or any number in it, and never any supplier rating or years of experience.
- max_concessions_per_turn: integer 1 to 4.
- ranking_weights: non-negative importance for each of {{weight_keys_json}} (they need not sum to 1). Supplier quality only ever influences the final ranking and is never discussed: give location_match at least as much weight as tenure_years (how long the supplier has been trading), and tenure_years at least as much as rating_bayes (the supplier's customer rating), i.e. location_match >= tenure_years >= rating_bayes. The three values must still fit the personality.
- commercial_terms: the company's own purchasing terms. The allowed ranges are in this JSON (key "terms"):
{{bands_json}}
  For a "numeric" term pick one number between lo and hi (inclusive). For a "set" term pick a list of between min_k and max_k values copied exactly from options. "tight_end" tells you which end is the more demanding one: "lo" means a lower number is more demanding, "hi" means a higher number is more demanding, "fewer" means a shorter list is more demanding. Give exactly the terms listed under "terms" and no others (terms under "omitted" must not appear). Meaning of each term: budget_inr = the most the company will pay in total, in INR; qty_vs_moq = how far above the required quantity a supplier's minimum order quantity may be, as a fraction of the quantity (0.25 = up to 25 percent above); preferred_states = the Indian states it prefers suppliers from. Pick values that fit the personality and the product.
  {{tightness_hint}}

{{tau_guidance}}

Required bands. Every value you choose for these traits MUST lie inside its band (inclusive). The bands were assigned to make this company differ from the others in the batch, so do not drift toward the middle:
{{strata_guidance}}
Let the company's style and voice fields, its industry, the ranking_weights and the commercial terms be consistent with these bands: a very low tau and high tolerance means an easygoing, flexible buyer; a high tau, low tolerance and few relaxations means a demanding, precise one; high decision_randomness means moody and inconsistent, low means predictable; few patience_rounds means an impatient buyer.

{{avoid_guidance}}

Tau, tolerance, weights and commercial terms should fit the personality you describe. Return one JSON object with exactly these keys (commercial_terms holds only the terms allowed above):
{"company_name":"","industry":"","city":"","style":"","tau":0.0,"category_tolerance":[{"category":"","multiplier":0.0}],"counter_band":0.0,"patience_turns":0,"max_concessions_per_turn":0,"max_relaxations":0,"patience_rounds":0,"decision_randomness":0.0,"tolerance":[{"category":"","value":0.0}],"voice":{"formality":"","verbosity":"","regional_voice":""},"sales_persona":{"name":"","speaking_style":"","sales_approach":""},"ranking_weights":{"spec_match":0.0,"price_score":0.0,"moq_fit":0.0,"rating_bayes":0.0,"tenure_years":0.0,"location_match":0.0,"evidence_confidence":0.0},"commercial_terms":{"budget_inr":0.0,"qty_vs_moq":0.0,"preferred_states":[""]}}
