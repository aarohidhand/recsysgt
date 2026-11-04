# Canonical Data Analysis Guide

## GeM Buyer/Bid Data and IndiaMART Product/Supplier Data

**Status:** Current-file review and retained-column decision\
**Scope:** Only the following supplied files are covered:

-   `final_constraints.csv`
-   `gem_bids_metadata.csv`
-   `products.csv`
-   `suppliers.csv`
-   `products_with_constraints.csv`
-   `product_constraints.csv`

The following files are intentionally excluded from this guide:
`supplier_products.csv`, `bid_constraints.csv`, and
`matching_constraints.csv`.

This guide records decisions based on the current files and their
present relationships. It does not add a future implementation plan.
Where a field is constant, empty, or currently carries no
differentiating information, it is removed from the retained operational
columns as requested; it can be recreated from the pipeline or source if
it becomes useful later.

------------------------------------------------------------------------

## 1. Canonical data relationship

The current data is best understood as four linked data domains:

1.  **Bid metadata** --- one row per GeM bid in `gem_bids_metadata.csv`.
2.  **Bid constraints** --- one row per extracted requirement in
    `final_constraints.csv`.
3.  **Product catalogue** --- one row per IndiaMART product in
    `products.csv`; enriched product evidence is represented in
    `products_with_constraints.csv`, and extracted constraints are
    represented as rows in `product_constraints.csv`.
4.  **Supplier profile** --- one row per supplier in `suppliers.csv`.

### Join relationships

  -------------------------------------------------------------------------------------------------------------------
  From                      Key             To                                  Key             Relationship and
                                                                                                purpose
  ------------------------- --------------- ----------------------------------- --------------- ---------------------
  `gem_bids_metadata.csv`   `bid_number`    `final_constraints.csv`             `bid_id`        Joins bid-level
                                                                                                metadata to the
                                                                                                individual buyer
                                                                                                requirements.

  `gem_bids_metadata.csv`   `bid_number`    `products.csv`                      `gem_bid_id`    Associates discovered
                                                                                                product candidates
                                                                                                with the GeM bid they
                                                                                                were collected for.

  `products.csv`            `product_id`    `products_with_constraints.csv`     `product_id`    Connects a catalogue
                                                                                                product to its
                                                                                                enriched product
                                                                                                representation.

  `products.csv`            `supplier_id`   `suppliers.csv`                     `supplier_id`   Connects each product
                                                                                                to the supplier
                                                                                                profile.

  `products_with_constraints.csv` `product_id` `product_constraints.csv` `product_id` Connects enriched product records to their extracted candidate-side constraints.
  -------------------------------------------------------------------------------------------------------------------

Use these exact identifiers as the join keys. Product and supplier names
are descriptive values, not safe primary keys. Preserve `product_url`
and supplier URLs as source references. The current product files use
IndiaMART product IDs as `product_id`; keep that ID stable across the
product-level CSVs.

The `golden_param` field in `final_constraints.csv` is **essential**. It
is the project's determinant for whether a GeM requirement is hard or
soft and must be retained without renaming or dropping it. It is not a
disposable extraction or retrieval parameter.

### Important interpretation

A product being associated with a bid means it was collected as a
candidate for that bid. It does not, by itself, mean the product
satisfies the bid. Product fit and supplier suitability must be assessed
using the requirement values and the candidate-side evidence.

------------------------------------------------------------------------

## 2. File-by-file decisions

## 2.1 `final_constraints.csv`

**Current size:** 178 rows × 32 columns\
**Role:** Main GeM constraint table; one record per extracted
requirement.

### Retain

  ---------------------------------------------------------------------
  Header                             Reason
  ---------------------------------- ----------------------------------
  `bid_id`                           Links every requirement to the GeM
                                     bid.

  `constraint_id`                    Identifies the individual
                                     constraint record.

  `golden_param`                     **Critical:** determines hard/soft
                                     constraint status in this project.

  `attribute`                        Normalized attribute name used for
                                     comparison.

  `spec_name`                        Human-readable specification
                                     label; retain alongside
                                     `attribute` because it is not
                                     proven redundant.

  `buyer_value`                      The value required or stated by
                                     the buyer.

  `operator`                         Defines the comparison condition.

  `unit`                             Needed to interpret and compare
                                     numeric values.

  `allowed_values`                   Preserves allowed alternatives
                                     where present.

  `allows_higher`                    A comparison-rule flag that varies
                                     in the current data.

  `constraint_category`              Broad grouping of the requirement.

  `constraint_type`                  More specific requirement
                                     classification.

  `scope`                            Distinguishes product, supplier,
                                     and transaction requirements.

  `status`                           Distinguishes specified and
                                     conditional records in the current
                                     data.

  `condition`                        Retain because it can encode the
                                     condition under which a
                                     requirement applies, despite being
                                     populated in only one current
                                     record.

  `administrative_only`              Retain as a classification flag;
                                     its sparsity does not make the
                                     meaning irrelevant.

  `matchability`                     Records the intended matching
                                     treatment.

  `matching_relevance`               Records relevance to the matching
                                     process.

  `indiamart_matchable`              Indicates whether the requirement
                                     is intended to be matched against
                                     IndiaMART data.

  `requirement_description`          Preserves the requirement/group
                                     descriptor used in the current
                                     records.

  `requirement_group_id`             Connects requirements belonging to
                                     a group.

  `requirement_group_type`           Identifies the kind of requirement
                                     group.

  `line_item_id`                     Retains line-item context where
                                     represented.

  `raw_text`                         Original requirement text for
                                     interpretation and auditing.

  `confidence`                       Extraction confidence.

  `evidence_status`                  Indicates whether an evidence
                                     anchor is present.

  `evidence_anchor`                  Source-passage anchor; important
                                     for auditing the extracted
                                     requirement.

  `source_document`                  Source document reference/name.
  ---------------------------------------------------------------------

### Remove

  ---------------------------------------------------------------------
  Header                             Reason for removal
  ---------------------------------- ----------------------------------
  `_chunk_parts`                     Extraction/chunk-processing
                                     diagnostic rather than a
                                     requirement attribute; it does not
                                     contribute to the current
                                     bid-to-product/supplier
                                     relationship.

  `_extraction_chunk_id`             Constant value `CHUNK_001` across
                                     all 178 rows; no distinguishing
                                     value in this file.

  `source`                           Constant value `qwen` across all
                                     rows; does not distinguish records
                                     in the current file.

  `source_document_id`               Constant value `MAIN_BID` across
                                     all rows; no differentiating value
                                     in this file. The more informative
                                     `source_document` field is
                                     retained.
  ---------------------------------------------------------------------

### Notes

-   The current file contains 178 constraints across six bids.
-   `golden_param` has both `True` and `False` values and must remain
    intact.
-   `evidence_anchor` is missing for some records. Keep the column and
    preserve missingness; do not delete the associated constraints
    merely because an anchor is absent.
-   `condition` and `administrative_only` are sparse, but are not
    removed because they carry distinct semantic meaning when populated.
-   Do not replace `golden_param` with an inferred value based on
    `constraint_type`, `scope`, or `matchability`.

------------------------------------------------------------------------

## 2.2 `gem_bids_metadata.csv`

**Current size:** 1,140 rows × 28 columns\
**Role:** Bid-level metadata. The six bids represented in the current
`final_constraints.csv` can be linked using `bid_number` to `bid_id`.

### Retain

  ---------------------------------------------------------------------
  Header                             Reason
  ---------------------------------- ----------------------------------
  `bid_id`                           Internal metadata record
                                     identifier; retain for
                                     traceability.

  `bid_number`                       Human-readable GeM bid identifier
                                     and join key to
                                     `final_constraints.bid_id` and
                                     `products.gem_bid_id`.

  `bid_year`                         Bid time/context field.

  `buyer_org`                        Buyer organization.

  `department`                       Buyer/administrative context.

  `product_category`                 Source-provided product/category
                                     name.

  `category_family`                  Broader category grouping.

  `quantity`                         Procurement quantity; relevant to
                                     candidate suitability and
                                     commercial comparison.

  `evaluation_type`                  Retain because it varies when
                                     populated.

  `bid_start_date`                   Bid timing context.

  `bid_end_date`                     Bid closing/timing context.

  `b_status`                         Varies in the current file and can
                                     distinguish bid states.

  `b_buyer_status`                   Varies where populated; retain as
                                     bid/buyer workflow context.

  `buyer_org`                        Buyer identity context; retained
                                     once in the header list.

  `bid_document_url`                 Main bid document source
                                     reference.

  `documents`                        Retains the available document
                                     collection/metadata.

  `other_document_urls`              Retain because this field may
                                     contain additional source
                                     documents and is not uniformly
                                     empty in the current file.
  ---------------------------------------------------------------------

### Remove

  ---------------------------------------------------------------------
  Header                             Reason for removal
  ---------------------------------- ----------------------------------
  `bid_type`                         Constant value `2` across all
                                     1,140 rows; no current
                                     discriminative value.

  `procurement_class`                Constant `UNKNOWN`; provides no
                                     classification signal in the
                                     current file.

  `classification_status`            Constant `PENDING`; no current
                                     differentiation across records.

  `classification_method`            Empty across all rows.

  `classification_confidence`        Empty across all rows.

  `pdf_review_status`                Constant `NOT_REVIEWED`; no
                                     current differentiation across
                                     records.

  `technical_spec_url`               Empty across all rows.

  `atc_url`                          Empty across all rows.

  `boq_url`                          Empty across all rows.

  `corrigendum_urls`                 Always the empty list `[]` in the
                                     current file.

  `eligibility_urls`                 Always the empty list `[]` in the
                                     current file.

  `catalogue_urls`                   Always the empty list `[]` in the
                                     current file.
  ---------------------------------------------------------------------

### Notes

-   `bid_number` is the reliable join field for the current
    bid-to-constraint and bid-to-product relationships.
-   Keep `product_category` as the source-provided category/name; do not
    treat it as a full technical specification.
-   The document URL fields removed above are empty in the current file.
    If populated in a later source version, they can be recreated or
    restored from that source.
-   This is a retained-column decision for the supplied version, not a
    claim that those fields are never useful in any future collection.

------------------------------------------------------------------------

## 2.3 `products.csv`

**Current size:** 686 rows × 47 columns\
**Role:** Main IndiaMART product catalogue and candidate records.

### Retain

  -----------------------------------------------------------------------
  Header                               Reason
  ------------------------------------ ----------------------------------
  `product_id`                         Stable product identifier used to
                                       link the catalogue and enriched product CSV.

  `product_url`                        Product source URL and identity
                                       cross-check.

  `product_name`                       Listed product name.

  `category_name`                      IndiaMART category context.

  `product_target`                     Bid/product target context used
                                       during candidate collection.

  `gem_bid_id`                         Links the candidate product to the
                                       GeM bid.

  `search_keyword`                     Records the search query that
                                       discovered the candidate; useful
                                       for traceability of candidate
                                       generation.

  `relevance_score`                    Retain as a candidate retrieval
                                       signal, not as proof of
                                       compliance.

  `price`                              Product price field.

  `price_formatted`                    Preserves the displayed price text
                                       and unit/context.

  `moq`                                Minimum order quantity where
                                       available.

  `moq_type`                           Unit/type of the minimum order
                                       quantity.

  `description`                        Product description evidence.


  `page_supplier`                      Supplier name displayed on the
                                       product page; useful for
                                       cross-checking supplier linkage.

  `specifications`                     Product specification attributes.

  `supplier_facts`                     Supplier facts extracted from the
                                       product page; retain as source
                                       evidence, distinct from the
                                       normalized supplier master.

  `jsonld_raw`                         Structured product-page source
                                       data; useful for auditing
                                       conflicting or missing extracted
                                       fields.

  `brochure_candidate_pdfimage_urls`   Retain as a source pointer for
                                       brochure/image-based technical
                                       evidence.

  `image_urls`                         Product images/source references.

  `brochure_status`                    Varies and indicates whether
                                       brochure processing was absent,
                                       extracted, or failed.

  `brochure_source_type`               Distinguishes brochure source
                                       types in the current records.

  `brochure_page_urls`                 Page-level brochure source
                                       references where available.

  `brochure_pages`                     Retain the current extracted page
                                       count/metadata field.


  `brochure_ocr_required`              Indicates whether OCR was
                                       required; varies in the current
                                       data.

  `brochure_text`                      Extracted brochure text used as
                                       technical evidence.

  `brochure_page_text`                 Page-segmented brochure text for
                                       traceability.

  `supplier_id`                        Foreign key to
                                       `suppliers.supplier_id`.

  `supplier_name`                      Supplier name associated with the
                                       product record.


  `supplier_url`                       Supplier storefront/source URL
                                       where available.

  `supplier_website_url`               Supplier website reference where
                                       available.

  `scraped_at`                         Product record
                                       freshness/provenance timestamp.
  -----------------------------------------------------------------------

### Remove

  ---------------------------------------------------------------------
  Header                             Reason for removal
  ---------------------------------- ----------------------------------
  `discovered_via`                   Constant `keyword_search` across
                                     all 686 records; no current
                                     differentiating value.

  `relevance_method`                 Constant `generic_lexical` across
                                     all records; no current
                                     differentiating value.

  `currency`                         All non-empty values are `INR`;
                                     deterministic in the current file
                                     and can be restored from
                                     source/pipeline if needed.

  `page_currency`                    All non-empty values are `INR`;
                                     deterministic in the current file.

  `page_availability`                All non-empty values are the same
                                     `https://schema.org/InStock`; no
                                     variation in the current records.

  `brochure_candidate_pdf_urls`      Empty list for every record.

  `http_status`                      Constant `200` across all rows; no
                                     current differentiation.

  `scrape_status`                    Constant `success` across all
                                     rows; no current differentiation.

  `scrape_error`                     Empty across all rows.

  `brochure_urls`                    Empty list for every record.

  `brochure_content_type`            Every populated value is `image`;
                                     no variation in the current file.

  `brochure_has_native_text`         Constant `False` across all rows.

  `brochure_extraction_method`       Every populated value is `ocr`; no
                                     variation among populated records.
  ---------------------------------------------------------------------

### Notes

- `price` and `page_price` are identical across all 686 rows in the current CSV; retain `price` and remove `page_price` as a duplicate.
- `brochure_pages` and `brochure_page_count` are identical across all 686 rows in the current CSV; retain `brochure_pages` and remove `brochure_page_count`.
- `supplier` and `supplier_name` are identical across all 686 rows in the current CSV; retain `supplier_name` and remove `supplier`.
-   Fields removed because they are constant or empty are not asserted
    to be universally useless; they simply do not differentiate the
    current supplied data and can be recreated later if required.
-   `brochure_candidate_pdfimage_urls` is retained because it is a
    distinct source pointer from the always-empty PDF candidate list.

------------------------------------------------------------------------

## 2.4 `suppliers.csv`

**Current size:** 254 rows × 20 columns\
**Role:** Supplier-level master/profile table.

### Retain

  ---------------------------------------------------------------------
  Header                             Reason
  ---------------------------------- ----------------------------------
  `supplier_id`                      Stable supplier key used by the
                                     product records.

  `supplier_native_id`               Source-native supplier identifier;
                                     useful for identity resolution and
                                     audit.

  `supplier_name`                    Supplier display name.

  `supplier_url`                     Supplier storefront/source URL.

  `supplier_website_url`             External supplier website where
                                     available.

  `city`                             Supplier location attribute.

  `state`                            Supplier location attribute.

  `gst_number`                       Retain only in the current
                                     restricted data layer for identity
                                     reconciliation; do not expose
                                     unnecessarily in model outputs.

  `rating`                           Supplier rating feature where
                                     available.

  `rating_count`                     Context for interpreting the
                                     rating.

  `member_since`                     IndiaMART membership
                                     tenure/context.

  `storefront_status`                Varies and describes storefront
                                     collection/availability status.

  `first_seen_category`              Category context in which the
                                     supplier was first observed.

  `first_seen_at`                    First-observed timestamp.
  ---------------------------------------------------------------------

### Remove

  ---------------------------------------------------------------------
  Header                             Reason for removal
  ---------------------------------- ----------------------------------
  `supplier_url_lookup_attempted`    Constant `False` across all
                                     records; no current
                                     differentiating value.

  `gst_verified`                     Constant `1` across all records;
                                     no current variation. Recreate
                                     from the source if the collection
                                     begins capturing meaningful
                                     variation.

  `supplier_id_source`               Constant `native_id` across all
                                     records; no current
                                     differentiation.

  `storefront_attempts`              All populated values are `1`; no
                                     variation among populated records.

  `storefront_product_count`         All populated values are `0`; no
                                     variation among populated records.

  `storefront_checked_at`            Collection diagnostic timestamp,
                                     not a current supplier suitability
                                     attribute; remove from the
                                     operational supplier profile.
  ---------------------------------------------------------------------

### Notes

-   There are 254 supplier records and 251 suppliers linked from the
    current product catalogue.
-   `gst_number` is retained for controlled identity reconciliation
    only. Restrict access and avoid using raw registration identifiers
    as recommendation features.
-   `storefront_status` is retained because it varies; the constant
    collection-attempt fields are removed.
-   The removed fields can be reconstructed if collection diagnostics
    are needed again.

------------------------------------------------------------------------

## 2.5 `products_with_constraints.csv`

**Current size:** 633 rows × 49 columns in the conversion output.
**Role:** Enriched product-level table, one row per product. Nested objects and lists are serialized as JSON strings in their cells by the conversion script.

### Retain

| Header | Reason |
|---|---|
| `product_id` | Stable product key for linking to `products.csv` and `product_constraints.csv`. |
| `product_url` | Product source reference and identity cross-check. |
| `product_name` | Listed product name. |
| `supplier_id` | Foreign key to `suppliers.csv`. |
| `supplier_name` | Supplier display name associated with the product. |
| `supplier_url` | Supplier storefront reference. |
| `supplier_website_url` | Supplier website reference where available. |
| `category_name` | Product category context. |
| `product_target` | Bid/candidate target context. |
| `gem_bid_id` | Link to the GeM bid for which the product was collected. |
| `search_keyword` | Records the query that discovered the candidate. |
| `relevance_score` | Candidate retrieval signal; not a compliance label. |
| `price` | Product price. |
| `price_formatted` | Displayed price string and context. |
| `moq` | Minimum order quantity where available. |
| `moq_type` | MOQ unit/type. |
| `description` | Product description evidence. |
| `page_supplier` | Supplier label displayed on the product page; useful for cross-checking the supplier link. |
| `specifications` | Product specification attributes, serialized as JSON where nested. |
| `supplier_facts` | Supplier facts extracted from the product page, serialized as JSON where nested. |
| `jsonld_raw` | Structured product-page source data for audit. |
| `brochure_candidate_pdfimage_urls` | Source pointer for brochure/image-based technical evidence. |
| `image_urls` | Product image references. |
| `brochure_status` | Brochure processing result; varies in the current records. |
| `brochure_source_type` | Brochure source classification. |
| `brochure_page_urls` | Page-level brochure source references. |
| `brochure_pages` | Brochure page metadata. |
| `brochure_ocr_required` | Indicates whether OCR was required. |
| `brochure_text` | Extracted brochure text. |
| `brochure_page_text` | Page-segmented brochure text for traceability. |
| `constraint_extraction` | Extraction status and provenance; keep for tracking successful and failed extraction. |
| `scraped_at` | Collection timestamp and provenance. |

### Remove

| Header | Reason for removal |
|---|---|
| `discovered_via` | Constant `keyword_search` in the current records; no differentiating value. |
| `relevance_method` | Constant `generic_lexical`; no differentiating value. |
| `currency` | All populated values are `INR`; deterministic in this snapshot. |
| `page_currency` | All populated values are `INR`; deterministic in this snapshot. |
| `page_availability` | All populated values are the same in-stock schema URL. |
| `brochure_candidate_pdf_urls` | Empty list across current records. |
| `http_status` | Constant `200` in the current records. |
| `scrape_status` | Constant `success` in the current records. |
| `scrape_error` | Empty across current records. |
| `brochure_urls` | Empty list across current records. |
| `brochure_content_type` | Every populated value is `image`. |
| `brochure_has_native_text` | Constant `False`. |
| `brochure_extraction_method` | Every populated value is `ocr`. |
| `constraints` | The nested constraint list is represented in normalized rows in `product_constraints.csv`; retaining it in the operational product table would duplicate the same constraint payload. |
| `page_price` | Identical to `price` across the current enriched CSV records; retain `price` as the canonical product price field. |
| `brochure_page_count` | Redundant with `brochure_pages` in the current records; retain `brochure_pages`. |
| `supplier` | Redundant with `supplier_name` in the current records; retain `supplier_name`. |

### Notes

- The conversion currently produces 633 product rows. The broader `products.csv` has 686 rows, so the enriched representation is not yet complete.
- `constraint_extraction` is retained as a serialized object in this product-level CSV. Its current statuses include 631 successful records and two failed records.
- The converter's output includes raw columns before canonical filtering. The retained header list in Section 4 is the intended operational schema after removing the fields listed above.

---

## 2.6 `product_constraints.csv`

**Current size:** 8,488 rows × 23 columns in the conversion output.
**Role:** One row per extracted constraint, with product, supplier and bid identifiers repeated so each constraint can be traced to its candidate.

### Retain

| Header | Reason |
|---|---|
| `product_id` | Product foreign key; primary link to the product-level record. |
| `supplier_id` | Supplier foreign key; links the constraint evidence to the supplier profile. |
| `gem_bid_id` | Bid foreign key; identifies the buyer request for which the product was collected. |
| `constraint_id` | Extracted constraint identifier; use with `product_id` if IDs are not globally unique. |
| `category` | Broad constraint category. |
| `scope` | Indicates whether the attribute applies to product or supplier scope. |
| `constraint_type` | Constraint classification. |
| `attribute` | Normalized attribute name. |
| `spec_name` | Human-readable specification name. |
| `operator` | Comparison operator. |
| `value` | Extracted constraint value. |
| `allowed_values` | Permitted alternatives where applicable. |
| `min_value` | Lower bound where applicable. |
| `max_value` | Upper bound where applicable. |
| `unit` | Unit of measurement. |
| `raw_text` | Original source text supporting the extracted constraint. |
| `raw_text_verified` | Indicates whether the source text was verified. |
| `confidence` | Extraction confidence. |
| `source_field` | Source field from which the constraint was extracted. |

### Remove

| Header | Reason for removal |
|---|---|
| `product_url` | Redundant with `product_id` and recoverable from the product-level table. |
| `product_name` | Redundant descriptive value; recoverable from the product-level table using `product_id`. |
| `supplier_name` | Redundant descriptive value; recoverable from `suppliers.csv` using `supplier_id`. |
| `constraint_index` | Deterministic row sequence generated only for export; no semantic value and can be recreated if needed. |

### Notes

- The converted file contains 8,488 extracted constraint rows.
- Keep the raw text and confidence fields so constraint values can be audited rather than treated as unquestioned facts.
- The extracted product/supplier constraints are not the same as GeM buyer constraints in `final_constraints.csv`; they are candidate-side evidence and must be compared against buyer requirements.

---

## 3. Cross-file retention rules

1.  **`golden_param` stays.** It is the determinant of hard versus soft
    status in the GeM constraint data and is central to the benchmark.
2.  **Identifiers stay.** Keep `bid_number`/`bid_id`, `gem_bid_id`,
    `product_id`, and `supplier_id` as the join keys in their respective
    files.
3.  **Source evidence stays.** Retain bid source text/anchors, product
    URLs, source-page fields, JSON-LD, brochure text and extraction
    provenance where specified above.
4.  **Do not equate retrieval with compliance.** `relevance_score`
    explains candidate retrieval; it does not establish that a product
    satisfies `golden_param` requirements.
5.  **Do not discard sparse semantic fields just for missingness.**
    Fields such as `condition`, `administrative_only`, `moq`, `unit`,
    and brochure text can be important when populated.
6.  **Remove current constants and empty columns from the retained
    operational headers.** The removed fields are either fully empty,
    constant in this snapshot, or collection diagnostics without current
    value for the product/supplier/bid relationship. They can be
    recreated from the collection pipeline if needed.
7.  **Do not merge supplier attributes into product identity.** Products
    and suppliers are separate entities connected by `supplier_id`.
8.  **Use the two converted CSVs for analysis.** `products_with_constraints.csv` is the product-level view; `product_constraints.csv` is the one-row-per-constraint view. Nested evidence fields in the product-level CSV remain JSON-serialized strings unless separately normalized.

------------------------------------------------------------------------

## 4. Final retained headers by file

These lists are the decided retained columns for the supplied versions
of the files. Header order follows the canonical order recommended for
analysis rather than necessarily the original source order.

### 4.1 `final_constraints.csv`

``` text
bid_id
constraint_id
golden_param
attribute
spec_name
buyer_value
operator
unit
allowed_values
allows_higher
constraint_category
constraint_type
scope
status
condition
administrative_only
matchability
matching_relevance
indiamart_matchable
requirement_description
requirement_group_id
requirement_group_type
line_item_id
raw_text
confidence
evidence_status
evidence_anchor
source_document
```

### 4.2 `gem_bids_metadata.csv`

``` text
bid_id
bid_number
bid_year
buyer_org
department
product_category
category_family
quantity
evaluation_type
bid_start_date
bid_end_date
b_status
b_buyer_status
bid_document_url
documents
other_document_urls
```

### 4.3 `products.csv`

``` text
product_id
product_url
product_name
supplier_id
supplier_name
supplier_url
supplier_website_url
category_name
product_target
gem_bid_id
search_keyword
relevance_score
price
price_formatted
moq
moq_type
description
page_supplier
specifications
supplier_facts
jsonld_raw
brochure_candidate_pdfimage_urls
image_urls
brochure_status
brochure_source_type
brochure_page_urls
brochure_pages
brochure_ocr_required
brochure_text
brochure_page_text
scraped_at
```

### 4.4 `suppliers.csv`

``` text
supplier_id
supplier_native_id
supplier_name
supplier_url
supplier_website_url
city
state
gst_number
rating
rating_count
member_since
storefront_status
first_seen_category
first_seen_at
```

### 4.5 `products_with_constraints.csv`

```text
product_id
product_url
product_name
supplier_id
supplier_name
supplier_url
supplier_website_url
category_name
product_target
gem_bid_id
search_keyword
relevance_score
price
price_formatted
moq
moq_type
description
page_supplier
specifications
supplier_facts
jsonld_raw
brochure_candidate_pdfimage_urls
image_urls
brochure_status
brochure_source_type
brochure_page_urls
brochure_pages
brochure_ocr_required
brochure_text
brochure_page_text
constraint_extraction
scraped_at
```

### 4.6 `product_constraints.csv`

```text
product_id
supplier_id
gem_bid_id
constraint_id
category
scope
constraint_type
attribute
spec_name
operator
value
allowed_values
min_value
max_value
unit
raw_text
raw_text_verified
confidence
source_field
```

------------------------------------------------------------------------

## 5. Final decision summary

-   **Bid/buyer side:** `final_constraints.csv` +
    `gem_bids_metadata.csv`.
-   **Product side:** `products.csv` as the broad catalogue and
    `products_with_constraints.csv` as the enriched product-level view;
    `product_constraints.csv` contains one row per extracted candidate-side constraint.
-   **Supplier side:** `suppliers.csv` as the supplier master.
-   **Most important bid-side field:** `golden_param`, retained as the
    hard/soft constraint determinant.
-   **Join keys:** `bid_number` ↔ `bid_id`/`gem_bid_id`; `product_id`;
    `supplier_id`.
-   **Removed fields:** current constants, empty columns, and
    non-discriminating collection diagnostics as listed above.
-   **Processing status:** `products_with_constraints.csv` currently has 633 product rows compared with 686 in `products.csv`; the enriched product set is incomplete. `product_constraints.csv` currently has 8,488 extracted constraint rows.
