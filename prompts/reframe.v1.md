---
name: reframe
version: 1
purpose: Decide per buyer constraint whether a private/commercial company would keep, translate or drop it, and rewrite it in commercial wording without changing any value.
input_vars: [product_category, constraints_json]
output_schema: ReframeOutput
temperature_key: reframe
---
You convert purchase requirements that came from a government procurement notice into requirements stated by a PRIVATE COMMERCIAL COMPANY that wants to buy "{{product_category}}". Output JSON only.

Input: a list of constraints, each with idx, golden_param, attribute, spec_name, operator, value, unit, allowed_values, condition, category.

For EACH constraint return one item with:
- idx, golden_param, operator, value, unit: copy EXACTLY from the input. Never change a number, unit, operator, value or golden_param.
- framing_decision:
  - "keep": already fine for a private company (technical specs, materials, standards such as ISI/IS/BEE, quantities, delivery days, test reports).
  - "translate": the idea is valid but the wording is governmental. Rewrite it in commercial wording (the government word for the party who submits an offer -> "supplier", "tender" and its synonyms -> "purchase", "offer validity" -> "quotation validity").
  - "drop": only meaningful in government procurement: EMD, ePBG, performance bank guarantee, tender security, MSE/MII/Make-in-India/local-supplier preference, OEM authorisation for the tender, GeM terms, submission/closing dates, order splitting, ministry/department/consignee/office addresses, reporting officers.
- commercial_text: one plain sentence a private buyer would say, stating the requirement with the same value and unit. Must be null for "drop". It must NOT contain: GeM, tender, EMD, ePBG, ministry, department, consignee, Make in India, MSE, MII, L1, reverse auction, offer validity.
- reason: at most 12 words explaining the decision.

Do not mention how strict a requirement is (no "must"/"ideally"); keep commercial_text neutral.

Example item: {"idx":0,"golden_param":true,"operator":"=","value":"15","unit":"days","framing_decision":"keep","commercial_text":"Delivery within 15 days.","reason":"plain delivery term"}

Constraints:
{{constraints_json}}

Return {"items":[{"idx":0,"golden_param":true,"operator":"","value":"","unit":"","framing_decision":"keep","commercial_text":"","reason":""}]} with exactly one item per input idx.
