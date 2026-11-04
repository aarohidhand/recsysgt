---
name: normalise
version: 1
purpose: Convert free-text catalogue values into a numeric value plus canonical unit, or null when not stated.
input_vars: [items_json]
output_schema: NormaliseOutput
temperature_key: normalise
---
Convert each free-text product value into one number and a canonical unit. Output JSON only.

Each input item has: item_id, text, target (what is being measured), canonical_unit.
Rules:
- Convert to canonical_unit when a known conversion exists (1 kW = 1.341 HP, 1 HP = 0.746 kW; mA to A divide by 1000; cm to mm multiply by 10). Round to at most 3 decimals.
- value is a plain number as text, for example "1.5". For a range write "low..high" (for example "3..15"). For "up to X" or "max X" write X.
- If the text does not state the measured quantity (for example "as per requirement", "customised", a different quantity, empty), value = null and unit = null. Never guess.
- If the text lists several alternatives ("5.5 kW/7.5 HP") use the one already in canonical_unit, else convert the first.
- confidence 0 to 1.
Return one result per item_id.

Example: {"item_id":0,"value":"1.5","unit":"HP","confidence":0.95}

Items:
{{items_json}}

Return {"items":[{"item_id":0,"value":null,"unit":null,"confidence":0.0}]}.
