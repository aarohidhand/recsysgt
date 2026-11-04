---
name: reveal_check
version: 1
purpose: Determine which brief constraint IDs a buyer utterance actually communicated.
input_vars: [utterance, constraints_json]
output_schema: RevealOutput
temperature_key: reveal_check
---
A buyer wrote the message below. Decide which of the listed requirements the message actually communicated, meaning it states that requirement and its value clearly enough that a reader would know it. A vague mention without the value does not count. Requirements not mentioned do not count. Output JSON only.

Message:
{{utterance}}

Requirements (id and text):
{{constraints_json}}

Return {"revealed_ids":[...]} containing only ids from the list above.
