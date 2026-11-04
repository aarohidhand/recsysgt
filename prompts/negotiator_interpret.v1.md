---
name: negotiator_interpret
version: 1
purpose: Classify, per item of the negotiator's own last proposal, whether the buyer's reply (last transcript turn) accepted, refused, countered or left it unclear.
input_vars: [transcript_json, proposal_items_json]
output_schema: InterpretOutput
temperature_key: negotiator_interpret
---
You are a negotiator reading the buyer's latest reply to your last proposal. Decide, for each proposed item, what the buyer said. Output JSON {"items": [{"index": 0, "response": "ACCEPT", "counter_value": null}, ...]} only.

Conversation so far (the buyer's reply is the last turn): {{transcript_json}}

Items you proposed (index, attribute, kind, from_value, to_value):
{{proposal_items_json}}
Kinds: "numeric_widen" and similar mean the buyer was asked to move from from_value to to_value; "drop" means to waive the requirement; "doc_to_pending" means the supplier shows the document later instead of meeting it now.

Rules:
- Return exactly one entry for every index listed, no extra and no repeated indexes.
- response is one of: ACCEPT (the buyer agrees to that change), REFUSE (the buyer keeps the original requirement), COUNTER (the buyer offers a different value than to_value), UNCLEAR (the reply does not address that item). Use UNCLEAR rather than guessing.
- If the buyer says yes or no to the whole proposal without singling out items, apply it to every item only when the wording clearly covers all of them; otherwise use UNCLEAR.
- counter_value: for COUNTER, the value the buyer offered, copied from the reply with its unit (never empty). For every other response, null.
- Judge only from the buyer's own words.
