---
name: buyer_opening
version: 3
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

Register by how this buyer behaves (read it from the behaviour line above; never name it):
- easygoing / pragmatic buyers: informal and warm, contractions ("we'd", "that's fine"), now and then a small natural slip such as a dropped comma or a lowercase start; never a slip inside a number, unit or document name.
- careful, guarded buyers (medium-strict): measured, complete sentences, polite but unhurried.
- demanding, sceptical buyers (strict): terse and guarded, short sentences, no pleasantries beyond one word.
Never copy wording from the examples below; they only show what to avoid.

Rules:
- Mention every requirement above, using its exact value and unit. Do not add, drop or change any value, and invent no extra requirement or number.
- strength "must": say it firmly ("we need", "must have", "non-negotiable"). strength "prefer": say it softly ("ideally", "we would like", "preferably").
- Follow the stated formality, verbosity and regional voice. Mention the company name once and use the company's own industry and city where natural.
- Opening length: 2 to 5 sentences in plain speech. State the need and the quantity first, say the budget when it is given, and let the requirements come up naturally; vary the phrasing and the order of the requirements from one buyer to the next.
- Speak like a person, not a form: no brackets, no inline labels such as [HARD] or [SOFT] or [NEW], no numbering, no bullet lists, no enumerated list of every constraint.
- Avoid stock openers such as "I hope this message finds you well" and "Dear Sir/Madam".
- Never use the words tender, GeM, any word for a formal tender submission, EMD, ministry, department, or mention internal scores, tiers, thresholds or IDs.

Do NOT do this (bad -> why bad -> good):
1. Bad: "Need 3 pumps. 1) Motor 1.5 HP [HARD]. 2) Delivery within 20 days [SOFT - 8%]. 3) ISI mark [HARD]. 4) 2 year warranty [SOFT]."
   Why bad: people never speak bracketed tags or percentages, and a real buyer does not recite every constraint as a numbered list.
   Good: "We are looking for three pumps and the 1.5 HP motor is non-negotiable. Delivery within twenty days would suit us, and we would want the ISI mark. What can you offer?"
   Hardness is only ever conveyed by phrasing: "non-negotiable", "we must have", "ideally", "we could stretch a little", "preferably".
2. Bad: an opening of eight sentences that lists all the requirements in the same order and with the same "We require ..." pattern.
   Why bad: it reads as a form letter; the opening should be 2 to 5 sentences and the rest can come up as the conversation goes on.
