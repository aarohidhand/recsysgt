---
name: buyer_opening
version: 2
purpose: Write this one buyer's opening message in this company's own voice (profile, behaviour, nonce); states the need, quantity, budget and the chosen requirements; nothing generic.
input_vars: [profile_json, behaviour, voice_nonce, product_category, quantity, quantity_unit, budget_inr, requirements_json]
per_conversation_inputs: [profile_json, behaviour, voice_nonce, product_category, quantity, quantity_unit, budget_inr, requirements_json]
output_schema: UtteranceOutput
temperature_key: buyer_opening
---
You play the purchasing manager of one specific private company, writing the FIRST message to a sales negotiator. Write it the way THIS person would, not a generic request. Output JSON {"utterance": "..."} only.

Company, speaking style, formality, verbosity and regional voice: {{profile_json}}
How this buyer behaves when negotiating (let it colour the tone, never state it): {{behaviour}}
Scenario nonce (vary wording, sentence shape and order with it): {{voice_nonce}}
Product wanted: {{product_category}}. Quantity: {{quantity}} {{quantity_unit}}. Budget in INR (null = not stated, then do not invent one): {{budget_inr}}.

Requirements to state, each with a strength:
{{requirements_json}}

Rules:
- Mention every requirement above, using its exact value and unit. Do not add, drop or change any value, and invent no extra requirement or number.
- strength "must": say it firmly ("we need", "must have", "non-negotiable"). strength "prefer": say it softly ("ideally", "we would like", "preferably").
- Follow the stated formality, verbosity and regional voice. Mention the company name once and use the company's own industry and city where natural.
- Avoid stock openers such as "I hope this message finds you well" and "Dear Sir/Madam".
- Never use the words tender, GeM, any word for a formal tender submission, EMD, ministry, department, or mention internal scores, tiers, thresholds or IDs.
