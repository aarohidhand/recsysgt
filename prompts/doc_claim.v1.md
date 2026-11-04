---
name: doc_claim
version: 1
purpose: Decide whether a supplier listing states conformity, certification or approval for a named document or standard, quoting the listing literally.
input_vars: [items_json]
output_schema: DocClaimOutput
temperature_key: doc_claim
---
For each item decide whether the supplier listing itself states that the product or supplier holds, meets or is certified or approved for the NAMED document or standard. Output JSON only.

Each input item has: item_id, doc_name, listing_text.
Rules:
- claims = true only if the listing text states conformity, certification, approval, licence or a test report for the named document or standard (for example "ISO 9001:2015 certified", "BIS licensed", "conforms to IS 302").
- quote = a literal quote copied character for character from listing_text that shows this statement (at most 200 characters). Never paraphrase, never add words, never combine separate places.
- claims = false and quote = null when the listing mentions only a different standard, only a generic word ("quality", "certified" with no document named), or nothing relevant. Never infer a claim that is not written.
- A standard mentioned only as a product feature name or as a requirement for the customer is not a claim by the supplier.
Return one result per item_id.

Example: {"item_id":0,"claims":true,"quote":"ISO 9001:2015 certified company"}
Example: {"item_id":1,"claims":false,"quote":null}

Items:
{{items_json}}

Return {"items":[{"item_id":0,"claims":false,"quote":null}]}.
