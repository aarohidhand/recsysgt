---
name: negotiator_extract
version: 1
purpose: Extract the buyer's requirements, with believed hardness from wording, from the conversation transcript only.
input_vars: [transcript_json]
output_schema: ExtractOutput
temperature_key: negotiator_extract
---
You are a sales negotiator reading a conversation with a buyer. List every product requirement the BUYER has stated, taking only what the buyer actually said. Output JSON only.

For each requirement return:
- attribute: short snake_case name (for example motor_rating, delivery_days, shaft_material, certification).
- operator: one of "=", ">=", "<=", ">", "<", "in", "required" (use "required" for yes/no features or documents).
- value: the value as text, exactly as stated (null if none). unit: the unit if any, else null.
- believed_hardness: "HARD" if the wording is firm ("must", "need", "non-negotiable", "mandatory", "required"), "SOFT" if the wording is flexible ("ideally", "prefer", "would like", "nice to have", "if possible"). If the buyer later says an item is flexible or firm, use the latest statement.
- is_doc_proof: true if the buyer wants a certificate/test report/licence/document as proof.
- source_turn: the turn_idx of the buyer message where it was stated (or last updated).
Do not invent requirements. Do not list negotiator statements.

Transcript:
{{transcript_json}}

Return {"items":[{"attribute":"","operator":"=","value":"","unit":null,"believed_hardness":"HARD","is_doc_proof":false,"source_turn":0}]}.
