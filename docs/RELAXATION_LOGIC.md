# Relaxation Logic

How the engine decides whether a requirement may change, which changes to propose, how the buyer answers, and how an accepted change is applied and checked. It describes the code as of commit `dca95c4` plus the soft-coverage fix of 2026-10-28 (section 16.2).

`docs/COMBINED_DESIGN.md` is the authoritative design (sections 4a, 5, 7, 8, 14). If this file and the design disagree, the design wins and the code has a bug. Section 16 lists the places where the code's behaviour is surprising or wrong.

---

## 1. Overview

A supplier that is not VERIFIED can become VERIFIED in two ways:

| Path | What changes | What it needs |
|---|---|---|
| **Verification** | What we know about a product (an evidence overlay entry) | A supplier document answering an open check. The requirement does not change. |
| **Relaxation** | What the buyer requires (the rule's active threshold) | Policy gates, a buyer ACCEPT and `validate_transition`, all three |

The rule is **verify first, relax second**. The reference policy proposes a relaxation only when no evidence plan is left or the buyer's patience has run out (section 9).

The pipeline, per negotiator turn:

```
policy file ──► may_relax(c) ──► relaxable rules + level domains (buyer bounds applied)
                                   │
products ──► need tables (strict, hopeful) ──► joint Pareto front ──► front points
                                   │                                    │
                                   └─► reachable suppliers ──► k_eff     ▼
                                                              menu: PRESENT_OFFER items
                                                                         │ (negotiator picks one)
                                                                         ▼
                                                           buyer decision (simulated or human)
                                                                         │
                                         ACCEPT ──► Change record ──► validate_transition ──► new active value
                                         REJECT / COUNTEROFFER ──► bounds tighten ──► smaller domains next turn
```

Every step is deterministic Python (invariant 8). The only randomness is the buyer's accept draw, from `random.Random(f"{seed}:buyer")`, plus the personality sampled once from `random.Random(f"{seed}:personality")` (invariant 9).

### Code map

| Concern | Location |
|---|---|
| Policy file, `RelaxRule`, `may_relax` | `recsys/policy.py:48`, `recsys/policy.py:114`, `recsys/policy.py:157` |
| Relaxable rules, levels, need tables, fronts, reachability, analysis rows | `recsys/relaxation.py` |
| Stored rule record per scenario | `recsys/orchestrator.py:688` (`_build_scenario`) |
| Front point → menu record (changes, H3 conditions, `verified_after`) | `recsys/orchestrator.py:352` (`point_record`) |
| Menu assembly | `recsys/orchestrator.py:398`, `recsys/menu.py:112`, `recsys/menu.py:141` |
| Reference policy | `recsys/menu.py:179` (`reference_choice`) |
| Buyer personality and decision | `recsys/buyer.py:94`, `recsys/buyer.py:198` |
| Buyer turn wiring (simulated, human) | `recsys/orchestrator.py:898`, `:913`, `:943` |
| Applying a decision, bounds, domains | `recsys/state.py:436`, `:172`, `:126` |
| Validation of a rule change | `recsys/state.py:290` (`check_change`), `:344` (`validate_transition`) |
| Query-mode policy overrides | `recsys/query.py:256` |
| Tests | `tests/relaxation/test_relaxation.py`, fixtures `tests/fixtures/prn_c1.json`, `prn_c3.json` |

---

## 2. Vocabulary

| Term | Meaning |
|---|---|
| `c` | A requirement (rule), identified by `constraint_id` |
| Level | A relative change of the rule's **original** threshold, an exact `Fraction` (0.10 = 10%). Index 0 means no change. |
| `r_c` | The chosen level index for rule `c` |
| `L_c` | The top level index of rule `c` (`len(levels) - 1`) |
| `max_deviation` | The largest level a rule may use. Global levels above it are dropped for that rule. |
| Direction | `decrease` (a lower value becomes acceptable, from `>=` / `>`) or `increase` (a higher value becomes acceptable, from `<=` / `<`) |
| Weight | A positive integer, the cost multiplier of the rule |
| H3 condition | An optional condition attached to a rule's relaxation, for example `discount_fraction >= 0.10` |
| Domain | The level indices of `c` still open after the policy and the buyer's earlier decisions |
| Buyer bound | `{"fixed": k}` after ACCEPT (N1), `{"max": k}` after COUNTEROFFER (N3), `{"max": 0}` after REJECT (N2) |
| Need table | `need[p][c]` = the smallest level index at which product `p` passes rule `c`, or `None` |
| Coverage | A level vector `R` covers product `p` if `R[c] >= need[p][c]` for every relaxable **HARD** rule `c`. It covers supplier `s` if it covers at least one of `s`'s products. Soft rules never decide coverage (8.2). |
| Recovery | Suppliers covered by `R` that are not covered at the baseline (every relaxable HARD rule at the lowest value of its domain) |
| Front | The cheapest level vectors that recover more suppliers than any cheaper vector |
| `V_s` | The number of distinct VERIFIED suppliers |
| `k_eff` | `min(k, |non-INELIGIBLE suppliers ∪ relaxation-reachable suppliers|)` |

---

## 3. Configuration (`config/scenario_policy.toml`)

### 3.1 Gates

```toml
[gates]
allow_hard_constraint_relaxation = false               # master gate for every HARD relaxation
allow_hard_product_specification_relaxation = false    # extra gate for HARD rules with scope = product
immutable_kinds = ["documentary"]                      # never relaxed, whatever the gates
soft_default_relaxable = false                         # true: every numeric SOFT rule with a direction is relaxable
```

The product gate is an extra gate, not an override. A HARD product rule needs both gates.

### 3.2 Levels and defaults

```toml
[relaxation]
levels = [0.0, 0.02, 0.05, 0.10, 0.25, 0.50]
default_max_deviation = 0.25
default_weight = 1
max_relaxation_combinations = 1000000
```

### 3.3 Per-rule entries

```toml
[relaxation.hard."<constraint_id>"]     # or [relaxation.soft."<constraint_id>"]
max_deviation = "1/3"                   # optional; default default_max_deviation
levels = ["0", "1/3"]                   # optional; default the global levels
weight = 1                              # optional; default default_weight
direction = "decrease"                  # optional; default from the operator
condition = { attribute = "discount_fraction", operator = ">=", value = 0.10 }   # optional H3
```

The rule's levels are its own `levels` (or the global list) filtered to `<= max_deviation`. For example, the global list with `max_deviation = 0.25` gives `[0, 0.02, 0.05, 0.10, 0.25]`, so `L_c = 4`.

### 3.4 Load-time validation (`build_policy`, `recsys/policy.py:157`)

The loader raises `PolicyError` naming the field when:

- a level list is empty, does not start at 0, or is not strictly increasing;
- a number cannot be read as an exact fraction. Floats are read as written (`0.02` → `1/50`), and strings such as `"1/3"` are allowed;
- `max_deviation < 0`, or `weight` is not a positive integer;
- `direction` is not `increase` or `decrease`, or `condition` is not a table;
- `known_constraint_ids` is given and a `[relaxation.*]` key is not among them (`check_relaxation_keys`, `recsys/policy.py:257`).

### 3.5 What the default file allows

Both hard gates are off, and `[relaxation.soft]` and `[relaxation.hard]` are empty. **On real bids nothing is relaxable**:

- the authorized front is empty;
- `reachable_suppliers` is empty, so `k_eff` counts only suppliers that are not INELIGIBLE;
- the engine only ever shows the diagnostic front.

The PRN fixtures switch the gates on through `policy_overrides`. Query mode switches them on through `relaxation_overrides` (section 13). Which soft rules should be relaxable, and by how much, is an open decision (CLAUDE.md).

---

## 4. Which rules may relax

### 4.1 `may_relax(c)` (`recsys/policy.py:114`)

Checked in this order:

1. `kind` is in `immutable_kinds` → **false**, for any hardness and any gate. Documentary rules are never relaxed.
2. The rule is HARD:
   - `allow_hard_constraint_relaxation` is false → false;
   - `scope == "product"` and `allow_hard_product_specification_relaxation` is false → false;
   - otherwise true only if `constraint_id` is listed in `[relaxation.hard]`.
3. The rule is SOFT: true if `constraint_id` is listed in `[relaxation.soft]`, or if `soft_default_relaxable` is on and the rule is numeric with an inequality operator (`>=`, `>`, `<=`, `<`).

Hardness comes from `policy.requirement_hardness(req)`. That is the requirement's explicit `hardness` in query mode (the user's confirmed `must`), and otherwise `golden_param` through `policy.hardness()` (`"True"` → HARD). Hardness never depends on whether relaxing would help (invariant 7).

The engine applies the normaliser's scope to the raw requirement before calling `may_relax` (`relaxation.py:89`, `orchestrator.py:248`), so the product-specification gate sees the scope the normaliser assigned.

### 4.2 Extra conditions in the engine

`may_relax` true is necessary, not sufficient. A rule takes part in the search (`_relaxable_rules`, `relaxation.py:120`) only if also:

- it is APPLICABLE (NOT_APPLICABLE rules are excluded from fronts);
- it is not waived;
- it has a `RelaxRule`, either configured, or synthetic for `soft_default_relaxable` (`_rule`, `relaxation.py:101`).

It can use a level above 0 only if its value is a number and it has a direction (`_levels_of`, `relaxation.py:151`). A listed rule with `==`, a boolean, or a string value gets the domain `[0]`, so in effect it can't relax.

The stored per-scenario rule record (`_build_scenario`, `orchestrator.py:688`) carries `may_relax = permitted and RelaxRule exists and len(levels) > 1`, along with its `direction`, `levels` (as fraction strings), `weight` and `condition`. The buyer simulator and the validator read this record.

### 4.3 Constraint class

`constraint_class` (normaliser) is `CONDITIONALLY_RELAXABLE_HARD` only for HARD rules listed under `[relaxation.hard]`. Other HARD rules are `IMMUTABLE_HARD`, and SOFT rules are `SOFT`. The class is a label; the gates still decide.

---

## 5. Levels and thresholds

### 5.1 Arithmetic (`level_value`, `relaxation.py:158`; `level_target`, `state.py:112`)

```
level 0:  threshold = current active value (the original, unless a change was accepted)
level i:  threshold = original × (1 − levels[i])   if direction = decrease
          threshold = original × (1 + levels[i])   if direction = increase
```

- The calculation always starts from the **original** value, never from an already relaxed one. Changes don't compound.
- The arithmetic uses `Fraction`. The result is an `int` when the denominator is 1, otherwise a `float`.
- `level_value` (search) and `level_target` (state, validator) implement the same formula. The validator recomputes the value and rejects a change whose `new_value` differs.

### 5.2 Examples

| Rule | Level | New threshold |
|---|---|---|
| `warranty_years >= 3` (decrease) | 1/3 | 3 × 2/3 = **2** (int) |
| `oem_turnover_crore >= 1.0` (decrease) | 0.02 | 49/50 = **0.98** (float) |
| `delivery_days <= 30` (increase) | 0.10 | 30 × 11/10 = **33** (int) |
| `weight_gsm >= 685` (decrease) | 0.05 | 685 × 19/20 = **650.75** (float) |

### 5.3 Cost of a level

The cost is linear in the **level index**, not in the percentage:

```
cost(R) = Σ_c weight_c × r_c / L_c        (exact Fraction)
```

With the default levels and `max_deviation = 0.25` (`L_c = 4`), a 2% change costs 1/4, 5% costs 1/2, 10% costs 3/4 and 25% costs 1. For a rule that isn't configured (a synthetic default rule), `L_c` is the count of global levels `<= default_max_deviation`, minus 1 (`_rule_info`, `relaxation.py:213`).

---

## 6. Domains and buyer bounds

### 6.1 Domain of a rule

The domain is `0..L_c`, then narrowed by the buyer bound (`_bounded`, `relaxation.py:138`; `rule_domain`, `state.py:126`):

| Bound | Domain |
|---|---|
| none | `[0, 1, …, L_c]` |
| `{"fixed": k}` (ACCEPT) | `[k]` |
| `{"max": k}` (COUNTEROFFER at `k`, or REJECT with `k = 0`) | `[0 … k]` |

Rules that aren't relaxable have domain `[0]`.

### 6.2 How decisions narrow bounds (`narrow_bounds`, `state.py:172`)

| Decision | Effect on the bound of each affected rule |
|---|---|
| ACCEPT | Every changed rule in the proposal: `{"fixed": level}` (N1) |
| REJECT | The rules the buyer named, or every changed rule in the proposal if none are named: `{"max": 0}` (N2) |
| COUNTEROFFER | Each rule in `counter_levels`: `{"max": min(current max, level)}` (N3) |
| ACKNOWLEDGE / diagnostic | No change |

Bounds **only tighten**. A fixed bound is never changed again, and a `max` never rises. `validate_transition` enforces both (`state.py:418`). After every buyer decision the scenario's `domains` are recomputed from the rules and bounds (`recompute_domains`, `state.py:281`).

A REJECT therefore removes a rule from all later proposals. A COUNTEROFFER doesn't change the rule. It caps the levels the next front may use, so the negotiator can come back with a proposal at or below the buyer's level.

---

## 7. Need tables (`need_tables`, `relaxation.py:176`)

For every product of the bid, sorted by `product_id`:

1. **Drop the product** if it FAILs any APPLICABLE HARD rule that is not relaxable (and not waived), evaluated at the current active values with the evidence overlay. Relaxation can't save it.
2. For every relaxable rule `c`, walk the domain in ascending order. At each level `i`, evaluate the product against the threshold of level `i`:
   - PASS → `strict[p][c] = i`; `hopeful[p][c] = i` if not yet set; stop.
   - UNKNOWN → `hopeful[p][c] = i` if not yet set; keep walking.
   - FAIL or UNSUPPORTED_ATTRIBUTE → keep walking.
3. If no level passes, `strict[p][c]` stays `None`. If no level passes or is UNKNOWN, `hopeful[p][c]` stays `None`.

| Table | Treats UNKNOWN as | Used for |
|---|---|---|
| strict | not passing | front coverage and the Pareto test, reported recovery |
| hopeful | passing | `hopeful_recovery` on front points, and `reachable_suppliers` (`k_eff`) |

The tables have a column for every relaxable rule, soft ones included. The front and `reachable_suppliers` read only the HARD columns (8.2).

The evaluation uses `recsys.feasibility.evaluate` with the evidence overlay, so simulated supplier answers count. Observed catalogue PASS/FAIL is never overridden (invariant 4).

UNSUPPORTED_ATTRIBUTE counts as not passing in **both** tables. Only UNKNOWN counts as hopeful.

---

## 8. The joint Pareto front (`front`, `relaxation.py:222`)

### 8.1 Why joint

Rules are searched together, not one at a time. A product that misses two relaxable rules is recovered only by relaxing both. A one-rule-at-a-time search never finds it.

### 8.2 Which rules decide coverage

Only **HARD** relaxable rules. The drivers (`_front_for`, `relaxation.py:378`) pass `front` the domains of the HARD rules only, which is the same set `reachable_suppliers` uses. The reason is that recovery means a supplier stops being INELIGIBLE, and a soft rule never makes a product INELIGIBLE. Soft rules enter only the soft score.

Two consequences:

- A product that can never pass a relaxable soft rule is still recovered by the hard relaxation it needs.
- A soft change never appears in a front point. It would add cost without recovering anyone, so no point on a correct front could contain it.

Design section 5 says "covered by `R` iff `R[c] >= need[p][c]` for all `c`". This file reads `c` as the relaxable HARD rules (fixed 2026-10-28, section 16.2).

### 8.3 Algorithm (exact enumeration)

1. **Useful values per rule:** the lowest domain value plus every `need` value that occurs in either table. Other levels can't change coverage, so they are skipped.
2. **Coverage masks:** for each rule and useful value `v`, a bitmask of the products with `need[p][c] <= v`. Products with `need = None` are never in the mask.
3. **Baseline:** the vector of lowest useful values. Its covered products and suppliers are the baseline sets. Recovery is always measured against them.
4. **Enumerate** the Cartesian product of the useful values, provided its size is `<= max_relaxation_combinations`. For each combination:
   - skip it if it covers no product beyond the baseline, or no supplier beyond the baseline;
   - record `(cost, −recovered suppliers, −recovered products, level tuple)`.
5. **Sort** ascending on that key, then walk it. Keep a point only when its recovered-supplier count is **strictly greater** than every point kept so far.

The result is a staircase: each kept point is the cheapest way found to reach its recovery count. Ties are broken by more products, then the lexicographically smallest level tuple (rules sorted by `constraint_id`).

### 8.4 What a `FrontPoint` carries (`relaxation.py:53`)

| Field | Meaning |
|---|---|
| `levels` | `(constraint_id, level index)` for every relaxable HARD rule, sorted |
| `cost` | Exact `Fraction` |
| `strict_suppliers`, `strict_products` | Recovered under the strict table |
| `hopeful_suppliers`, `hopeful_products` | Recovered under the hopeful table (informational) |
| `diagnostic` | True for the diagnostic front |
| `conditions` | The H3 conditions of the rules that this point raises above their lowest domain value |

Inside the front, H3 conditions are **assumed to hold**. They are evaluated later, when the point becomes a menu record (section 10.2).

### 8.5 Large search spaces (`_front_cpsat`, `relaxation.py:302`)

Above `max_relaxation_combinations` the front comes from OR-Tools CP-SAT with `num_workers = 1` and a seed derived from the scenario seed. If OR-Tools is not installed, it raises a `RuntimeError` rather than falling back. OR-Tools 9.15 is installed in `venv/`.

For each target `t = 1, 2, …` recovered suppliers, the solver:

1. minimises cost subject to at least `t` suppliers recovered;
2. fixes that cost and maximises `10 000 × suppliers + products`;
3. fixes that and makes each `r_c` lexicographically minimal;
4. keeps the point if it recovers more suppliers than the last kept point.

`test_cpsat_matches_exact` checks that this matches the exact enumeration.

### 8.6 Worked example (from `tests/relaxation/test_relaxation.py`)

Two HARD rules, `A: alpha >= 10` and `B: beta >= 10`, each with levels `[0, 1/2]`, weight 1 and direction decrease. Level 1 gives a threshold of 5.

| Product | Supplier | alpha | beta | need A | need B |
|---|---|---|---|---|---|
| P1 | S1 | 5 | 5 | 1 | 1 |
| P2 | S2 | 5 | 10 | 1 | 0 |
| P3 | S3 | 10 | 5 | 0 | 1 |

| `(r_A, r_B)` | Covers | Suppliers recovered | Cost |
|---|---|---|---|
| (0, 0) | none (baseline) | — | 0 |
| (0, 1) | P3 | 1 | 1 |
| (1, 0) | P2 | 1 | 1 |
| (1, 1) | P1, P2, P3 | 3 | 2 |

Front: `(0, 1)` → 1 supplier at cost 1, then `(1, 1)` → 3 suppliers at cost 2. `(1, 0)` is dropped because it doesn't beat 1. Relaxing one rule at a time never recovers S1.

### 8.7 Authorized and diagnostic fronts

| Front | Rules included | Use |
|---|---|---|
| `authorized_front` | HARD rules with `may_relax` true (plus the conditions in 4.2) | `PRESENT_OFFER` menu items; can be applied |
| `diagnostic_front` | HARD rules that pass every condition **except** the gates. Documentary rules are still excluded. | `COUNTER_OFFER_DIAGNOSTIC`; informational only, never applied |

The diagnostic front answers "what would it take if policy allowed it". The buyer can only ACKNOWLEDGE it.

---

## 9. Reachability, `k_eff` and the reference policy

### 9.1 Reachable suppliers (`reachable_suppliers`, `relaxation.py:399`)

A supplier is reachable if at least one of its products, with every authorized rule at the highest level its domain still allows:

- does not FAIL any non-relaxable HARD rule (it survived step 1 of section 7), and
- has a non-`None` **hopeful** need for every relaxable HARD rule.

H3 conditions are assumed to hold. If no authorized rule has a domain with more than one level, the result is empty. That is always the case with the gates off.

`k_eff = min(k, |non-INELIGIBLE suppliers ∪ reachable suppliers|)` (`recsys/ranking.py:114`). It is recomputed after every state change. A REJECT that pins a rule to 0 can therefore lower `k_eff`.

### 9.2 Where relaxation sits in the policy

The reference policy (`reference_choice`, `menu.py:179`) picks, in order:

1. `k_eff = 0` → `END_NO_DEAL`.
2. `V_s >= k_eff` → `RECOMMEND`.
3. The evidence plan for `k_eff − V_s` suppliers (or the closest one), if any plan is on the menu. Plans appear only while buyer patience remains.
4. The first, which is the cheapest, `PRESENT_OFFER` item.
5. The first `COUNTER_OFFER_DIAGNOSTIC` item.
6. `RECOMMEND` if any supplier is not INELIGIBLE, else `END_NO_DEAL`.

So the reference policy relaxes only when no evidence plan is available, because no supplier is PROVISIONAL_RESOLVABLE, the planner skipped them all, or patience is exhausted (invariant 5).

The menu itself does not enforce this order. While `V_s < k_eff`, the front points are listed next to the evidence plans, and an LLM negotiator may choose a `PRESENT_OFFER` first. That choice is recorded with `agrees_with_reference = false`.

---

## 10. From front point to menu item

### 10.1 Selection (`Engine.build_menu`, `orchestrator.py:398`)

Only while `V_s < k_eff`:

- **Authorized points.** Each front point is turned into a record (10.2). It is kept only if it has at least one change and `verified_after > V_s`. At most `menu.max_front_points` (default 5) are kept.
- **Diagnostic points.** Only if no authorized point was kept and no diagnostic point has been shown in this scenario (`diagnostic_used`). At most `menu.max_diagnostic_points` (default 3), each with at least one change.

`verified_after` (`hypothetical_verified`, `orchestrator.py:298`) is the number of VERIFIED suppliers after applying the point's levels to the active values, with the current overlay and blocked requests. Strict recovery only says that the recovered suppliers pass the relaxed rules and fail no fixed hard rule. They may still have UNKNOWN checks elsewhere, so a point can have recovery > 0 and `verified_after == V_s`. Such a point is not offered.

### 10.2 The point record (`point_record`, `orchestrator.py:352`)

- **Changes:** only rules whose level is above the level the buyer has already accepted. Each change lists `attribute`, `unit`, `original_value`, `current_value`, `new_value` (from `level_target`), `level`, `level_fraction` and `percent`.
- **H3 conditions:** for each changed rule with a condition, the record stores `observed` and `met = condition_holds(cond, observed)`:
  - Only `discount_fraction` can be observed. Its value is the **smallest** discount among the point's recovered suppliers, and it exists only if every recovered supplier has an offer.
  - Offers come from fixture supplier truth: `1 − price_2yr / price_3yr`, stored in `var/hidden/<scenario>/extras.json`.
  - Any other condition attribute, or a missing offer, gives `observed = None` and `met = None`.
- **Recovery:** `recovered_suppliers`, `recovery_count`, `recovered_products`, `hopeful_recovery_count`, `verified_after`, `cost` and `discount_fraction`.

### 10.3 Menu item (`_point_item`, `menu.py:112`)

The item type is `PRESENT_OFFER` or `COUNTER_OFFER_DIAGNOSTIC`. Its parameters hold the record above, plus `verified_suppliers_now`, `k_eff`, `conditions_met` and, for diagnostics, `blocked_by: "policy gates (shown for information only; never applied)"`.

`allowed_numbers` lists every number the negotiator's message may quote: values, percentages, counts, condition values and observed values. The dialogue templates call these acts `CONDITIONAL_OFFER` and `FEASIBILITY_REPORT` (`recsys/dialogue.py`).

The negotiator packet never contains the buyer's personality, tolerance, patience or `max_relaxations` (`FORBIDDEN_KEYS`, `recsys/packets.py:28`).

---

## 11. The buyer's decision

### 11.1 The proposal the buyer sees (`_buyer_turn`, `orchestrator.py:913`)

- `levels`: the rules the point changes.
- `max_levels`: `L_c` per rule.
- `not_permitted` (`_not_permitted`, `orchestrator.py:898`): rules in the proposal for which any of these holds:
  - the proposal is diagnostic;
  - the stored record has `may_relax` false, or `policy_permits` (the current policy) refuses;
  - the level is outside the current domain;
  - an H3 condition does not hold (`met` is not `True`, so missing evidence counts as not holding).
- `diagnostic` and `discount_fraction`.

### 11.2 Personality (`sample_personality`, `buyer.py:94`)

Sampled once per scenario from the `personality` stream, in this fixed draw order:

| Trait | Range (`[buyer]`) | Meaning |
|---|---|---|
| hardness `H` | `[0.2, 0.8]` | Higher means less willing |
| `max_relaxations` | `[0, 3]` | Most distinct rules changed over the whole scenario |
| `patience_rounds` | `[1, 4]` | Evidence rounds tolerated before the menu stops offering plans |
| `decision_randomness` | `[0.1, 0.5]` | Flattens the accept curve |
| tolerance `T_c` | `[0, 5]`, per relaxable rule in sorted order | The highest level index the buyer accepts for `c` |

A rule missing from the tolerance map has `T_c = 0`. Fixtures pin the personality with `FIXTURE_BUYER` (`orchestrator.py:181`: `H = 0.5`, `max_relaxations = 3`, `T_c = 5`, …). The personality is stored in `var/hidden/` (invariant 11).

### 11.3 Decision steps (`decide`, `buyer.py:198`)

1. **Diagnostic proposal** → `ACKNOWLEDGE`.
2. **Policy** (`check_policy`): a changed rule is in `not_permitted` → `REJECT`, reason `policy`, naming those rules.
3. **Limit** (`check_limit`): the number of distinct rules accepted so far plus those changed now exceeds `max_relaxations` → `REJECT`, reason `limit`, no rule named. N2 then pins **every** rule in the proposal to 0.
4. **Fixture override:** if the fixture's `buyer_truth` has `accepts_<x>_when_discount_fraction_ge = t`, the decision is deterministic: ACCEPT if `discount_fraction >= t`, else REJECT naming every changed rule. No random draw.
5. **Tolerance:** if some changed rule has `r_c > T_c`, take the first such rule `c` (sorted):
   - if `min(T_c, L_c) >= 1` **and** the proposal changes exactly one rule → `COUNTEROFFER {c: min(T_c, L_c)}`;
   - otherwise → `REJECT`, reason `tolerance`, naming every rule over tolerance.
6. **Probabilistic accept:** draw `u` from the `buyer` stream; ACCEPT if `u < p`, else REJECT, reason `declined`, no rule named, so every rule in the proposal is pinned to 0.

```
p = sigmoid( α_eff · (1 − H − d) + β · V − κ )
α_eff = α · (1 − decision_randomness)                  α = 6.0, β = 2.0, κ = 0.0
d     = max over changed rules of  r_c / L_c           (0..1)
V     = log(1 + confirmed_recovery) / log(1 + pool)    clipped to [0, 1]
confirmed_recovery = V_s now − V_s at scenario start   (suppliers verified by evidence so far)
pool  = number of suppliers in the bid
```

Example: `H = 0.5` and `decision_randomness = 0.3` give `α_eff = 4.2`.

| Proposal | `d` | `V` | `p` |
|---|---|---|---|
| Half of the rule's range | 0.5 | 0 | `sigmoid(0)` = 0.50 |
| The rule's maximum | 1.0 | 0 | `sigmoid(−2.1)` ≈ 0.11 |

Each decision is logged to `var/hidden/<scenario>/decisions.json` with its accept probability.

### 11.4 Human buyer (query mode, `_human_buyer_turn`, `orchestrator.py:943`)

The user decides. The turn lists:

- `allowed_decisions`: ACKNOWLEDGE for a diagnostic. Otherwise ACCEPT (only if nothing is `not_permitted`), REJECT, and COUNTEROFFER (only if some counter option exists).
- `counter_options`: for each changed rule, the domain levels strictly between 0 and the proposed level, with their values.

The engine validates the response: the decision must be allowed, `counter_levels` must be among the options, and `counter_levels` is allowed only with COUNTEROFFER. The same `apply_buyer_decision` then runs.

---

## 12. Applying the decision and validating it

### 12.1 `apply_buyer_decision` (`state.py:436`)

- **Diagnostic:** only ACKNOWLEDGE is legal. It appends history and advances the turn; nothing else changes.
- **ACKNOWLEDGE on a non-diagnostic proposal:** rejected.
- **ACCEPT:** for each changed rule, in sorted order:
  - compute `new_value = level_target(rule, level)`;
  - append a `Change(constraint_id, original_value, new_value, level, actor="buyer", turn, justification)`, where the justification holds the decision, the proposal turn and the H3 condition records;
  - set the active value, record the accepted level, and add a `RULE_CHANGE` transition (`provenance: simulator`).
- **All decisions:** narrow the bounds (6.2), add a `BOUNDS` transition if they changed, recompute the domains, and commit through `validate_transition`. On any error the old state is returned unchanged.

The original requirement is never edited. The relaxation lives only in `active_values` and in the `accepted_changes` record (invariant 2).

### 12.2 `check_change` (`state.py:290`): every accepted change must satisfy

1. The rule exists, and its kind is not in `immutable_kinds`.
2. The stored record has `may_relax` true, **and** `policy_permits` passes against the current policy.
3. `original_value` equals the stored original.
4. `actor == "buyer"`. Supplier answers and LLM output can never change a rule (invariant 3).
5. `0 < level < len(levels)`, and the level is inside the domain left by earlier buyer decisions.
6. `new_value` equals the level value computed from the **original**, and the new active value equals `new_value`.
7. The latest history entry is a buyer **ACCEPT** of a **non-diagnostic** proposal that contains this exact level for this rule, and the rule is not in its `not_permitted`.
8. If the rule has an H3 condition: the justification carries a condition record whose attribute, operator and value equal the policy's, and `condition_holds(cond, observed)` is `True`. Unknown evidence fails.

### 12.3 `validate_transition` (`state.py:344`), the parts about rules

- Original rules unchanged.
- `accepted_changes` only grows. Earlier records are never rewritten.
- A rule can't change twice in one transition.
- Every moved active value has a matching new change record.
- Bounds: none lost, none loosened, no fixed bound changed.

---

## 13. Outcomes and ground truth

### 13.1 Outcome (`final_record`, `orchestrator.py:439`)

| Outcome | When |
|---|---|
| `RECOMMENDABLE` | RECOMMEND with `V_s >= k_eff`, every entry VERIFIED, and no HARD rule changed |
| `EXCEPTION_HANDLED` | The same, after at least one accepted change to a HARD rule (`relaxed_hard_applied`) |

A soft-only change keeps `RECOMMENDABLE`. The final record lists `relaxations_applied` (constraint ids) and `hard_relaxation_applied`.

### 13.2 Ground-truth tables (`recsys/ground_truth.py`)

- **Policy record:** the three gate values and `relaxable_rules`.
- **`relaxation_analysis`:** one block per state snapshot (`turn_id = t`), from `relaxation_analysis_rows` (`relaxation.py:419`). Each point of the authorized front and of the diagnostic front gives one row per changed rule, with:
  - `front` (`authorized` / `diagnostic`), `point`, `cost`;
  - `strict_recovery`, `hopeful_recovery`, the supplier lists, the product counts;
  - `constraint_id`, `old_value` (active value, else original), `proposed_value`, `level_index`, `level_fraction`;
  - `policy_permitted`, `diagnostic`, `provenance: inferred`.

  Because fronts cover over HARD rules only (8.2), no row proposes a soft change.
- **Outcome rows:** `relaxations_applied` and `hard_relaxation_applied`.

---

## 14. Query mode (COMBINED_DESIGN 14)

`relaxation_overrides` (`query.py:256`) builds a policy override when `[query] relax_numeric = true`:

- both hard gates are switched on;
- every non-documentary rule with an inequality operator and a numeric value, of either hardness, is listed under `[relaxation.hard]` or `[relaxation.soft]` with `max_deviation = default_max_deviation` (25%) and a direction from the operator (`>=`/`>` → decrease, `<=`/`<` → increase).

Hardness comes from the user's confirmed `must` flag. Soft rules are listed too, but they are never proposed (8.2, 16.1 item 1), so in practice only `must` rules are offered for change. No personality is sampled, the user is the buyer (11.4), and patience is the remaining `run.max_turns`. Every change still needs the user's ACCEPT and passes the same validator. Query runs are not benchmark ground truth.

---

## 15. Fixture walk-throughs

### 15.1 PRN-C1 (`tests/fixtures/prn_c1.json`, `k = 3`)

**Policy:** both gates on. `C1-WARRANTY` (`warranty_years >= 3 years`, HARD, product scope) is listed under `[relaxation.hard]` with:

- `levels = ["0", "1/3"]`, `max_deviation = 1/3`;
- `weight = 1`, `direction = decrease`;
- condition `discount_fraction >= 0.10`.

**Data:**

| Product | Supplier | Warranty | 3-year price | 2-year price | Discount |
|---|---|---|---|---|---|
| P-C1-A | S-A | 2 | 52 000 | 46 800 | 0.10 |
| P-C1-B | S-B | 2 | 50 000 | 45 000 | 0.10 |
| P-C1-C | S-C | 2 | 54 000 | 48 600 | 0.10 |

**Trajectory:**

1. **Turn 0.** Every product FAILs warranty (2 < 3); all other checks PASS. Every supplier is INELIGIBLE, so `V_s = 0`.
2. **Reachability.** `C1-WARRANTY` has domain `[0, 1]`, and level 1 gives threshold 3 × 2/3 = 2. Each product needs level 1, so all three suppliers are reachable and `k_eff = 3`.
3. **Front.** One point, `{C1-WARRANTY: 1}`, cost 1, recovering S-A, S-B and S-C. The condition is observed at min(0.10, 0.10, 0.10) = 0.10 and met; `verified_after = 3`.
4. **Menu.** There is no evidence plan, since nothing is PROVISIONAL_RESOLVABLE. The reference picks `PRESENT_OFFER`.
5. **Buyer.** The fixture's `buyer_truth` threshold is 0.10 and the discount is 0.10, so the buyer ACCEPTs deterministically.
6. **Apply.** `Change(C1-WARRANTY, 3 → 2, level 1, buyer)` is recorded. The bound becomes `{"fixed": 1}` and the validator passes. `V_s = 3`.
7. **End.** `RECOMMEND`, outcome **`EXCEPTION_HANDLED`** (hard relaxation; negB's `DEAL` is wrong here, see CLAUDE.md).

### 15.2 PRN-C3 (`tests/fixtures/prn_c3.json`, `k = 1`)

**Start:**

- Orion FAILs only warranty, so it is recoverable.
- Nova FAILs `bypass_tray`, which is not relaxable, so it is dropped from the need tables.
- Vega is PROVISIONAL_RESOLVABLE, with turnover and OEM authorisation UNKNOWN.
- `k_eff = 1`.

**Trajectory:**

1. Verify first: the reference policy requests Vega's documents before it offers Orion's warranty change.
2. The fixture makes Vega DECLINE. Vega's checks become NOT_REQUESTABLE and Vega is PROVISIONAL_BLOCKED.
3. No plan is left. The warranty point (3 → 2 years, recovering Orion, `verified_after = 1`) is offered and ACCEPTed.
4. `RECOMMEND` Orion, outcome `EXCEPTION_HANDLED`, with one evidence turn more than negB.

---

## 16. Known gaps and surprising behaviour

Each item was checked against the code on 2026-10-28.

### 16.1 Open

1. **Soft relaxations are never proposed.**
   - Fronts cover over HARD rules only (8.2), so no front point changes a soft rule. Tiers also depend only on HARD rules (`_hard`, `feasibility.py:539`), so a soft change couldn't raise `V_s` in any case.
   - Listing a rule under `[relaxation.soft]`, or turning on `soft_default_relaxable`, has these effects only:
     - the rule's record gets `may_relax = true` and it appears in the ground-truth `relaxable_rules`;
     - it gets a soft column in the need tables;
     - the buyer personality draws a tolerance for it. Tolerances are drawn in sorted rule order, so adding a rule can shift the tolerances drawn for rules that sort after it.
   - The same applies to soft rules in query mode.
   - If soft relaxation should improve the ranking inside a tier, it needs its own objective. That belongs with the open decision on soft rules (CLAUDE.md).
2. **Strict recovery is not verification.** The front ranks by strict recovery (the relaxed rules pass, no fixed hard rule FAILs), but the menu keeps only points that raise `V_s`. A point whose recovered suppliers still have open UNKNOWN checks is dropped, even if one more evidence round would verify them. This matches design section 5 step 3 ("raises `V_s`"), but the search and the filter use different measures.
3. **Equal-count alternatives are dropped.** The front keeps one point per recovery count, the cheapest, then the lexicographically smallest tuple. A point of the same cost recovering a *different* set of suppliers never appears, so the negotiator can't choose between them.
4. **H3 conditions.**
   - Only `discount_fraction` can be observed, and only from fixture offers. On real bids, a rule with any condition has `met = None`, is marked `not_permitted`, and draws a `REJECT (policy)`. That pins it to 0.
   - The observed discount is taken from hidden fixture data and shown in the negotiator's menu before any supplier turn has stated it.
5. **COUNTEROFFER only for single-rule proposals.** A multi-rule proposal over tolerance is rejected outright, naming the rules over tolerance, rather than countered.
6. **A non-tolerance REJECT pins every rule in the proposal.** Rejections for `limit`, `declined` or the fixture threshold name no rules, or all of them, so all rules in the proposal are pinned to 0 for the rest of the scenario.
7. **`V` in the accept probability** uses suppliers verified since the start of the scenario (`V_s − initial V_s`), not the recovery the proposal promises. Design section 7 calls it `confirmed_supplier_recovery`, which fits this reading, but a reader might expect the proposal's own recovery.
8. **UNSUPPORTED_ATTRIBUTE is not hopeful.** A relaxable rule with no usable field on a product gives `need = None` in both tables, so that product is neither recoverable nor reachable through relaxation. It is not INELIGIBLE at level 0 either.
9. **Verify-first is a property of the reference policy, not of the menu.** An LLM negotiator can choose a `PRESENT_OFFER` while evidence plans are still available (9.2).

### 16.2 Fixed

**A relaxable SOFT rule could hide a HARD recovery** (fixed 2026-10-28, `_front_for`, `relaxation.py:378`).

*Before:* front coverage required `R[c] >= need[p][c]` for every relaxable rule, soft ones included. That caused two problems:

- A product that could never pass a relaxable soft rule (`need = None`) was never covered, even when a hard relaxation made it eligible. With HARD `A: alpha >= 10` and SOFT `S: sigma >= 10`, both relaxable, and one product (`alpha = 5`, `sigma = 1`):
  - `authorized_front` returned `[]`;
  - `reachable_suppliers` returned `{S1}`, which counted toward `k_eff`;
  - so the menu had no offer for a supplier that `k_eff` was waiting on.
- A soft change could look like a recovery. A product already eligible but failing a soft rule at level 0 was "recovered" by relaxing that soft rule. This put false points on the front and false rows in the ground-truth `relaxation_analysis` table, and soft changes could ride along with the hard ones in an offer.

*After:* only HARD relaxable rules decide coverage (8.2), matching `reachable_suppliers`. The need tables still have the soft columns.

*Visible effect in query mode:* the pump sample from `tests/orchestrator` has `power >= 15 hp` (must), plus a soft `warranty_years >= 1` that query mode lists as relaxable.

| Code | Authorized front | Reference offer |
|---|---|---|
| Before the fix | 15 → 11.25 hp (25%, level 4) only. Products without warranty data were never covered. | the 25% cut |
| After the fix | 15 → 14.7 hp (2%, level 1, cost 1/4, `V_s` 5 → 6), then 15 → 11.25 hp (25%, cost 1, `V_s` 5 → 8) | the 2% cut |

The reference now offers a 2% change where it used to offer 25%.

*Tests* (`tests/relaxation/test_relaxation.py`): both fail on the old code and pass on the new.

- `test_relaxable_soft_rule_does_not_block_hard_recovery`: the front is `[{A: 1}]` recovering S1, and is the same with or without `S` listed.
- `test_soft_only_change_recovers_nobody`: an eligible product failing only the soft rule gives an empty front.
- `tests/orchestrator/test_query_mode.py`:
  - `test_human_reject_changes_nothing` now asserts that the first reference offer is the 2% change, with no counter options.
  - The two human-buyer tests that need counter options (`test_human_accept_applies_relaxation_with_buyer_source_human`, `test_human_decision_outside_allowed_is_rejected`) used to get them from the buggy 25% offer. Their negotiator now picks the largest offer on the menu (`deepest_offer`), which is still the 25% change.

Ground-truth runs generated before the fix may contain the false rows in `relaxation_analysis` if their policy listed soft rules. The default policy lists none, so default runs are unaffected.

---

## 17. Invariants this logic upholds

| Invariant (CLAUDE.md) | Where it is enforced |
|---|---|
| 2: originals immutable, relaxation is a separate record | `apply_buyer_decision` writes `Change` records; `validate_transition` rejects edits to `rules` |
| 3: rule changes need `may_relax`, buyer ACCEPT of a non-diagnostic proposal, validator | `check_change` items 2, 4 and 7; `_not_permitted`; `decide` step 2 |
| 5: verify before relax | `reference_choice` order (plans before `PRESENT_OFFER`) |
| 7: hardness from `golden_param` / `must`, never from usefulness | `policy.requirement_hardness` |
| 8: deterministic Python | All of `relaxation.py`, `buyer.py`, `state.py`; the LLM only picks a menu item |
| 9: seeded randomness | `personality` and `buyer` streams only; CP-SAT seeded, single worker |
| 10: provenance | Analysis rows `inferred`; rule-change and bounds transitions `simulator` |
| 11: hidden data | Personality, decisions and offers under `var/hidden/`; forbidden keys stripped from packets |


---
## Amendment 2026-11-01 (D076): preferred_states is relaxable
Supersedes the 'categorical SOFT has domain [0]' behaviour for the supplier-state preference only. Levels: 0 original preferred set; 1 add up to `relaxation.location.level1_max_added_states` adjacent/common
supplier states; 2 drop the state preference. Cost weight `relaxation.location.cost_weight` per level. The rule enters the same joint front as every other relaxable rule; the buyer decision uses the
same sigmoid with the persona's own LOCATION tolerance (strict personas scaled by `strict_resistance`). Other categorical SOFT rules keep domain [0].
