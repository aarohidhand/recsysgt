---
name: doc_classify
version: 1
purpose: Classify whether a constraint is satisfied by showing a document or certificate rather than by a measurable product value.
input_vars: [constraints_json]
output_schema: DocClassifyOutput
temperature_key: doc_classify
---
For each purchase requirement below decide whether a supplier would normally satisfy it by SHOWING A DOCUMENT OR CERTIFICATE (for example ISO certificate, BIS/ISI licence, test report, calibration certificate, warranty card, GST certificate, OEM authorisation letter, datasheet, brochure) instead of by a product value that can be compared numerically or by name.

Rules:
- is_doc_proof = true only when the requirement is about certification, a licence, a test report, a registration or a stated document. A material, size, power rating, speed or similar physical specification is false, even if a standard is cited for it.
- doc_name: short name of the document (for example "ISO 9001 certificate", "BIS licence", "type test report", "warranty card"). Null when is_doc_proof is false.
Output JSON only.

Input fields per item: idx, attribute, spec_name, operator, value, category.

Example: {"idx":3,"is_doc_proof":true,"doc_name":"type test report"}

Items:
{{constraints_json}}

Return {"items":[{"idx":0,"is_doc_proof":false,"doc_name":null}]} with exactly one item per input idx.
