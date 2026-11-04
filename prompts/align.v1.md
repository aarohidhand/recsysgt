---
name: align
version: 1
purpose: Map each buyer attribute to the candidate-side attribute names that describe the same specification, with confidence.
input_vars: [buyer_json, candidate_attrs_json]
output_schema: AlignOutput
temperature_key: align
---
Map buyer requirement attributes to supplier-catalogue attribute names that describe THE SAME specification. Output JSON only.

Buyer attributes (buyer_attribute, spec_name, value, unit):
{{buyer_json}}

Catalogue attributes, each written "attribute|spec_name" (spec_name is the original label, possibly empty):
{{candidate_attrs_json}}

Rules:
- For each buyer attribute return up to 3 matches. candidate_attribute must be copied exactly from the part BEFORE the "|" of a catalogue entry.
- Match only when the meaning is the same (for example motor_rating ~ motor_horsepower, power_hp, power_rating; discharge_head ~ head, maximum_head). Do not match unrelated things. If nothing fits return an empty matches list.
- confidence from 0 to 1: 0.9+ clear synonym, 0.6 to 0.8 probable, below 0.5 weak (omit weak ones).

Example: {"buyer_attribute":"bore_size","matches":[{"candidate_attribute":"borewell_size","confidence":0.85}]}

Return {"items":[{"buyer_attribute":"...","matches":[{"candidate_attribute":"...","confidence":0.0}]}]} with one item per buyer attribute. Use exactly these key names and no others (do not echo spec_name or value).
