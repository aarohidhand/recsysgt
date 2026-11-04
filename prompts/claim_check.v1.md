---
name: claim_check
version: 1
purpose: List the factual supplier/product claims in a negotiator utterance and the evidence id supporting each, or unsupported.
input_vars: [utterance, evidence_json]
output_schema: ClaimOutput
temperature_key: claim_check
---
Below is a message from a sales negotiator and a list of evidence snippets from the catalogue. List every FACTUAL claim the message makes about a specific supplier or product (price, rating, city, specification, certification, warranty, minimum order, availability, years in business). Do not list opinions, greetings, questions, or statements about the buyer's own requirements.

For each claim give the evidence_id of the snippet that supports it. If no snippet supports it exactly, evidence_id = "unsupported". A snippet that contradicts the claim does not support it. Output JSON only.

Message:
{{utterance}}

Evidence:
{{evidence_json}}

Return {"claims":[{"claim":"...","evidence_id":"..."}]}; use {"claims":[]} if there are no factual claims.
