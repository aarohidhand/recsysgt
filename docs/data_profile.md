# Data Profile

golden_param True means: **HARD** (settings.yaml).

## Row counts

| Table | Rows | Retained columns |
|---|---|---|
| final_constraints | 178 | 28 |
| gem_bids_metadata | 1140 | 16 |
| products | 686 | 31 |
| suppliers | 254 | 14 |
| product_constraints | 9123 | 19 |
| products_with_constraints | 685 | 32 |

## Per-bid candidates and golden_param

Candidates = distinct product_id / supplier_id in products.csv via products.gem_bid_id = final_constraints.bid_id. Retrieval is not compliance.

| bid_id | constraints | golden True | golden False | numeric SOFT (any scope) | numeric SOFT (product-matchable) | cand. products | cand. suppliers | cand. with extracted constraints | cand. constraint rows |
|---|---|---|---|---|---|---|---|---|---|
| GEM/2019/B/209245 | 22 | 19 | 3 | 2 | 0 | 90 | 40 | 90 | 1158 |
| GEM/2019/B/231682 | 28 | 11 | 17 | 7 | 1 | 77 | 26 | 77 | 1064 |
| GEM/2019/B/242710 | 38 | 27 | 11 | 3 | 2 | 216 | 63 | 216 | 3089 |
| GEM/2019/B/341930 | 22 | 22 | 0 | 0 | 0 | 135 | 53 | 135 | 1831 |
| GEM/2020/B/488257 | 57 | 49 | 8 | 1 | 0 | 60 | 41 | 60 | 737 |
| GEM/2020/B/626099 | 11 | 11 | 0 | 0 | 0 | 108 | 37 | 107 | 1244 |

## Join orphan counts

Each row: distinct keys on each side and how many have no partner on the other side. Names are never join keys.

| Join (left <-> right) | distinct left | left orphans | distinct right | right orphans |
|---|---|---|---|---|
| gem_bids_metadata.bid_number <-> final_constraints.bid_id | 1140 | 1134 | 6 | 0 |
| final_constraints.bid_id <-> products.gem_bid_id | 6 | 0 | 6 | 0 |
| gem_bids_metadata.bid_number <-> products.gem_bid_id | 1140 | 1134 | 6 | 0 |
| products.product_id <-> product_constraints.product_id | 686 | 1 | 685 | 0 |
| products.product_id <-> products_with_constraints.product_id | 686 | 1 | 685 | 0 |
| products_with_constraints.product_id <-> product_constraints.product_id | 685 | 0 | 685 | 0 |
| products.supplier_id <-> suppliers.supplier_id | 251 | 0 | 254 | 3 |
| products_with_constraints.supplier_id <-> suppliers.supplier_id | 251 | 0 | 254 | 3 |
| product_constraints.supplier_id <-> suppliers.supplier_id | 251 | 0 | 254 | 3 |
| product_constraints.gem_bid_id <-> final_constraints.bid_id | 6 | 0 | 6 | 0 |
| products_with_constraints.gem_bid_id <-> final_constraints.bid_id | 6 | 0 | 6 | 0 |

products rows: 686; distinct product_id: 686; distinct (product_id, gem_bid_id): 686; (product_id, gem_bid_id) in products_with_constraints but not products: 0; in products but not products_with_constraints: 1.

## Null rates of retained columns

Empty/NaN cells; missingness is preserved, never filled.

### final_constraints

| column | null count | null % |
|---|---|---|
| bid_id | 0 | 0.0% |
| constraint_id | 0 | 0.0% |
| golden_param | 0 | 0.0% |
| attribute | 0 | 0.0% |
| spec_name | 0 | 0.0% |
| buyer_value | 11 | 6.2% |
| operator | 0 | 0.0% |
| unit | 132 | 74.2% |
| allowed_values | 131 | 73.6% |
| allows_higher | 0 | 0.0% |
| constraint_category | 0 | 0.0% |
| constraint_type | 0 | 0.0% |
| scope | 0 | 0.0% |
| status | 0 | 0.0% |
| condition | 177 | 99.4% |
| administrative_only | 176 | 98.9% |
| matchability | 0 | 0.0% |
| matching_relevance | 0 | 0.0% |
| indiamart_matchable | 0 | 0.0% |
| requirement_description | 0 | 0.0% |
| requirement_group_id | 0 | 0.0% |
| requirement_group_type | 0 | 0.0% |
| line_item_id | 47 | 26.4% |
| raw_text | 0 | 0.0% |
| confidence | 0 | 0.0% |
| evidence_status | 0 | 0.0% |
| evidence_anchor | 60 | 33.7% |
| source_document | 0 | 0.0% |

### gem_bids_metadata

| column | null count | null % |
|---|---|---|
| bid_id | 0 | 0.0% |
| bid_number | 0 | 0.0% |
| bid_year | 0 | 0.0% |
| buyer_org | 447 | 39.2% |
| department | 82 | 7.2% |
| product_category | 0 | 0.0% |
| category_family | 0 | 0.0% |
| quantity | 0 | 0.0% |
| evaluation_type | 1018 | 89.3% |
| bid_start_date | 0 | 0.0% |
| bid_end_date | 0 | 0.0% |
| b_status | 0 | 0.0% |
| b_buyer_status | 295 | 25.9% |
| bid_document_url | 0 | 0.0% |
| documents | 0 | 0.0% |
| other_document_urls | 0 | 0.0% |

### products

| column | null count | null % |
|---|---|---|
| product_id | 0 | 0.0% |
| product_url | 0 | 0.0% |
| product_name | 0 | 0.0% |
| supplier_id | 0 | 0.0% |
| supplier_name | 0 | 0.0% |
| supplier_url | 351 | 51.2% |
| supplier_website_url | 335 | 48.8% |
| category_name | 0 | 0.0% |
| product_target | 0 | 0.0% |
| gem_bid_id | 0 | 0.0% |
| search_keyword | 0 | 0.0% |
| relevance_score | 0 | 0.0% |
| price | 39 | 5.7% |
| price_formatted | 39 | 5.7% |
| moq | 311 | 45.3% |
| moq_type | 34 | 5.0% |
| description | 70 | 10.2% |
| page_supplier | 39 | 5.7% |
| specifications | 0 | 0.0% |
| supplier_facts | 0 | 0.0% |
| jsonld_raw | 0 | 0.0% |
| brochure_candidate_pdfimage_urls | 0 | 0.0% |
| image_urls | 0 | 0.0% |
| brochure_status | 0 | 0.0% |
| brochure_source_type | 0 | 0.0% |
| brochure_page_urls | 0 | 0.0% |
| brochure_pages | 626 | 91.3% |
| brochure_ocr_required | 0 | 0.0% |
| brochure_text | 628 | 91.5% |
| brochure_page_text | 626 | 91.3% |
| scraped_at | 0 | 0.0% |

### suppliers

| column | null count | null % |
|---|---|---|
| supplier_id | 0 | 0.0% |
| supplier_native_id | 0 | 0.0% |
| supplier_name | 0 | 0.0% |
| supplier_url | 129 | 50.8% |
| supplier_website_url | 125 | 49.2% |
| city | 0 | 0.0% |
| state | 0 | 0.0% |
| gst_number | 0 | 0.0% |
| rating | 21 | 8.3% |
| rating_count | 21 | 8.3% |
| member_since | 0 | 0.0% |
| storefront_status | 0 | 0.0% |
| first_seen_category | 0 | 0.0% |
| first_seen_at | 0 | 0.0% |

### product_constraints

| column | null count | null % |
|---|---|---|
| product_id | 0 | 0.0% |
| supplier_id | 0 | 0.0% |
| gem_bid_id | 0 | 0.0% |
| constraint_id | 0 | 0.0% |
| category | 0 | 0.0% |
| scope | 0 | 0.0% |
| constraint_type | 0 | 0.0% |
| attribute | 0 | 0.0% |
| spec_name | 865 | 9.5% |
| operator | 0 | 0.0% |
| value | 1237 | 13.6% |
| allowed_values | 0 | 0.0% |
| min_value | 8260 | 90.5% |
| max_value | 8109 | 88.9% |
| unit | 5421 | 59.4% |
| raw_text | 0 | 0.0% |
| raw_text_verified | 0 | 0.0% |
| confidence | 0 | 0.0% |
| source_field | 0 | 0.0% |

### products_with_constraints

| column | null count | null % |
|---|---|---|
| product_id | 0 | 0.0% |
| product_url | 0 | 0.0% |
| product_name | 0 | 0.0% |
| supplier_id | 0 | 0.0% |
| supplier_name | 0 | 0.0% |
| supplier_url | 351 | 51.2% |
| supplier_website_url | 334 | 48.8% |
| category_name | 0 | 0.0% |
| product_target | 0 | 0.0% |
| gem_bid_id | 0 | 0.0% |
| search_keyword | 0 | 0.0% |
| relevance_score | 0 | 0.0% |
| price | 39 | 5.7% |
| price_formatted | 39 | 5.7% |
| moq | 311 | 45.4% |
| moq_type | 34 | 5.0% |
| description | 70 | 10.2% |
| page_supplier | 39 | 5.7% |
| specifications | 0 | 0.0% |
| supplier_facts | 0 | 0.0% |
| jsonld_raw | 0 | 0.0% |
| brochure_candidate_pdfimage_urls | 0 | 0.0% |
| image_urls | 0 | 0.0% |
| brochure_status | 0 | 0.0% |
| brochure_source_type | 0 | 0.0% |
| brochure_page_urls | 0 | 0.0% |
| brochure_pages | 626 | 91.4% |
| brochure_ocr_required | 0 | 0.0% |
| brochure_text | 628 | 91.7% |
| brochure_page_text | 626 | 91.4% |
| constraint_extraction | 0 | 0.0% |
| scraped_at | 0 | 0.0% |

## JSON parse failures (D019)

| table | column | failures | non-empty cells |
|---|---|---|---|
| final_constraints | allowed_values | 0 | 47 |
| gem_bids_metadata | documents | 0 | 1140 |
| gem_bids_metadata | other_document_urls | 0 | 1140 |
| products | specifications | 0 | 686 |
| products | supplier_facts | 0 | 686 |
| products | jsonld_raw | 0 | 686 |
| product_constraints | allowed_values | 0 | 9123 |
| products_with_constraints | specifications | 0 | 685 |
| products_with_constraints | supplier_facts | 0 | 685 |
| products_with_constraints | jsonld_raw | 0 | 685 |
| products_with_constraints | constraint_extraction | 0 | 685 |

Total parse failures: 0. Full list: data/canonical/parse_failures.csv.

## Pilot bid recommendation

Criteria: >= 3 candidate products (ideally >= 10), at least one HARD and one SOFT constraint, at least one numeric SOFT (golden_param=False) constraint that is product-matchable (operator, numeric buyer_value, unit present, matchability=product); transaction/supplier numerics such as delivery days or turnover cannot be widened against catalogue values and are not counted. Ranked by candidates having extracted constraints (so compliance is not all UNKNOWN), capped at 30, then numeric SOFT count, then constraint count.

**Recommended: `GEM/2019/B/242710`** - 216 candidate products from 63 suppliers (216 with extracted constraints, 3089 candidate constraint rows); 38 buyer constraints: 27 golden_param=True (HARD) and 11 golden_param=False; 2 numeric constraints with golden_param=False for relaxation search.

Numeric product-matchable golden_param=False constraints of the pilot bid (caveat: a `quantity` row is an order size, not a catalogue spec):

| constraint_id | attribute | operator | buyer_value | unit |
|---|---|---|---|---|
| GEM/2019/B/242710_C_0feff9086d88 | max_current | = | 4.5 | A |
| GEM/2019/B/242710_C_61c7c937b0f5 | motor_rating | = | 1.5 | HP |

Top alternatives (only 1 other bid(s) meet all criteria):

| bid_id | cand. products | with constraints | golden True/False | numeric SOFT (product-matchable) |
|---|---|---|---|---|
| GEM/2019/B/231682 | 77 | 77 | 11/17 | 1 |
