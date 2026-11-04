"""D053 relaxation rules: which constraints may relax, their levels (from the ORIGINAL value), and the relaxed
copy of a constraint at a level.  Pure, deterministic, exact (``Fraction``) arithmetic.

Translation of docs/RELAXATION_LOGIC.md to gtgen (D053 amendment):
* the rules that decide coverage are the relaxable SOFT negotiated rules (HARD only if
  ``relaxation.gates.allow_hard_relaxation``); doc-proof rules, verification_only / dropped rules and
  non-numeric rules are never relaxable (domain [0]);
* level ``i`` of a rule means a relative change of the ORIGINAL threshold of ``levels[i] * max_deviation``
  (settings ``relaxation.levels`` are "fractions of the allowed deviation"; set ``level_mode="relative"`` to read
  them as plain relative changes filtered by ``max_deviation``).  Level 0 = original value; changes never compound.
* direction: ``>=``/``>`` (and ``=`` with allows_higher) lower the threshold; ``<=``/``<`` raise it; numeric ``=``
  without allows_higher widens to a symmetric band (D026); ``range`` widens both ends.
* D076: the supplier-state preference (attribute ``preferred_states``) is a ``set`` rule: levels 0 = original set,
  1 = + up to ``relaxation.location.level1_max_added_states`` adjacent/common pool states, 2 = any state (drop the
  preference).  Level sets are nested (monotone widening).  Needs the candidate pool (``build_rules(candidates=...)``);
  without it the rule is not built (not relaxable).  Other categorical SOFT rules keep domain [0].
* cost of a level vector = sum_c weight_c * r_c / L_c  (exact Fraction).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Mapping, Optional, Sequence

from gtgen.contracts import BelievedConstraint, BuyerConstraint, CandidateView, Hardness, ScenarioRole
from gtgen.matching.compliance import is_inactive, role_of
from gtgen.matching.evidence import (
    ConstraintT,
    as_view,
    effective_op,
    hardness_of,
    parse_number_unit,
    parse_range,
    ref_of,
    target_number,
    _split_list,
)
from gtgen.matching.states import norm_state, widening_states

_DEFAULT_LEVELS = (Fraction(0), Fraction(1, 50), Fraction(1, 20), Fraction(1, 10), Fraction(1, 4), Fraction(1, 2))


def _frac(x: object) -> Fraction:
    return Fraction(str(x)) if not isinstance(x, Fraction) else x


@dataclass(frozen=True)
class RelaxConfig:
    allow_hard: bool = False
    allow_soft: bool = True
    soft_default_relaxable: bool = True
    allow_hard_product_spec: bool = False     # spec 3.1 second gate; stored only: the engine has no rule scope yet
    immutable_kinds: tuple[str, ...] = ("documentary",)   # never relaxed, whatever the gates
    default_weight: int = 1                   # default per-rule cost weight (spec 3.2)
    levels: tuple[Fraction, ...] = _DEFAULT_LEVELS
    default_max_deviation: Fraction = Fraction(1, 4)
    level_mode: str = "relative"              # "relative" (spec 5) | "fraction_of_max"
    max_combinations: int = 1000000
    menu_size: int = 5
    max_items: int = 4                        # negotiation.max_bundle_size: points changing more rules are dropped
    max_diagnostic_points: int = 3
    alpha: float = 6.0
    beta: float = 2.0
    kappa: float = 0.0
    tau_weight: float = 1.5     # D082
    bounds_tolerance: float = 0.0
    range_max_relaxations: tuple[int, int] = (0, 3)
    range_patience_rounds: tuple[int, int] = (1, 4)
    range_decision_randomness: tuple[float, float] = (0.1, 0.5)
    range_tolerance: tuple[float, float] = (0.0, 5.0)
    location_enabled: bool = False             # D076 (settings relaxation.location.*)
    location_max_added: int = 2
    location_cost_weight: Fraction = Fraction(3, 2)
    location_strict_resistance: float = 0.5
    location_category: str = "LOCATION"


def config_from_settings(settings: Optional[Mapping]) -> RelaxConfig:
    """``settings`` may be the full settings dict or its ``relaxation`` section."""
    full = settings or {}
    sec = full.get("relaxation", full) or {}
    d = RelaxConfig()
    gates = sec.get("gates", {}) or {}
    buyer = sec.get("buyer", {}) or {}
    pr = sec.get("persona_ranges", {}) or {}
    neg = full.get("negotiation", {}) or {}
    loc = sec.get("location", {}) or {}

    def rng(key, dflt, cast):
        v = pr.get(key)
        return (cast(v[0]), cast(v[1])) if v else dflt

    return RelaxConfig(
        allow_hard=bool(gates.get("allow_hard_constraint_relaxation", d.allow_hard)),
        allow_hard_product_spec=bool(gates.get("allow_hard_product_specification_relaxation",
                                               d.allow_hard_product_spec)),
        immutable_kinds=tuple(str(k) for k in gates.get("immutable_kinds", d.immutable_kinds)),
        soft_default_relaxable=bool(gates.get("soft_default_relaxable", d.soft_default_relaxable)),
        default_weight=int(sec.get("default_weight", d.default_weight)),
        levels=tuple(_frac(x) for x in sec["levels"]) if sec.get("levels") else d.levels,
        default_max_deviation=_frac(sec.get("default_max_deviation", d.default_max_deviation)),
        level_mode=str(sec.get("level_mode", d.level_mode)),
        max_combinations=int(sec.get("max_relaxation_combinations", d.max_combinations)),
        menu_size=int(sec.get("menu_size", d.menu_size)),
        max_items=int(neg.get("max_bundle_size", sec.get("max_items", d.max_items))),
        alpha=float(buyer.get("alpha", d.alpha)), beta=float(buyer.get("beta", d.beta)),
        kappa=float(buyer.get("kappa", d.kappa)), tau_weight=float(buyer.get("tau_weight", d.tau_weight)),
        bounds_tolerance=float(sec.get("belief_bounds_tolerance", d.bounds_tolerance)),
        range_max_relaxations=rng("max_relaxations", d.range_max_relaxations, int),
        range_patience_rounds=rng("patience_rounds", d.range_patience_rounds, int),
        range_decision_randomness=rng("decision_randomness", d.range_decision_randomness, float),
        range_tolerance=rng("tolerance", d.range_tolerance, float),
        location_enabled=bool(loc.get("enabled", d.location_enabled)),
        location_max_added=int(loc.get("level1_max_added_states", d.location_max_added)),
        location_cost_weight=_frac(loc.get("cost_weight", d.location_cost_weight)),
        location_strict_resistance=float(loc.get("strict_resistance", d.location_strict_resistance)),
        location_category=str(loc.get("persona_category", d.location_category)),
    )


# ------------------------------------------------------------------ rule
@dataclass(frozen=True)
class RelaxRule:
    ref: str
    attribute: str
    hardness: Hardness
    mode: str                         # lower | upper | band | range | set (D076 state preference)
    original: Fraction                # target (band/lower/upper) or range lo
    original_hi: Optional[Fraction]   # range hi
    unit: Optional[str]
    original_value: str               # the constraint's value string
    levels: tuple[Fraction, ...]      # relative change per level index; levels[0] == 0
    weight: "int | Fraction" = 1
    category: Optional[str] = None
    level_sets: tuple[tuple[str, ...], ...] = ()   # set rules: allowed states per level (nested, level_sets[0] = original)
    added: tuple[tuple[str, ...], ...] = ()        # set rules: states added vs level 0, per level

    @property
    def top(self) -> int:
        """L_c: the highest level index."""
        return len(self.levels) - 1


def _rule_levels(cfg: RelaxConfig, override: Optional[Mapping]) -> tuple[Fraction, ...]:
    o = override or {}
    maxdev = _frac(o.get("max_deviation", cfg.default_max_deviation))
    base = tuple(_frac(x) for x in o["levels"]) if o.get("levels") else cfg.levels
    if not base or base[0] != 0 or any(b <= a for a, b in zip(base, base[1:])):
        raise ValueError("relaxation levels must start at 0 and be strictly increasing")
    if cfg.level_mode == "relative" or o.get("levels"):
        return tuple(x for x in base if x <= maxdev)
    return tuple(x * maxdev for x in base)


def may_relax(c: ConstraintT, cfg: RelaxConfig, roles: Optional[Mapping[str, ScenarioRole]] = None, *,
              ignore_gates: bool = False, overrides: Optional[Mapping[str, Mapping]] = None) -> bool:
    """Policy gates (spec 4.1/4.2 in gtgen terms).  ``ignore_gates`` serves the diagnostic front only."""
    if is_inactive(c) or role_of(c, roles) != ScenarioRole.NEGOTIATED:
        return False
    if c.is_doc_proof and "documentary" in cfg.immutable_kinds:
        return False                               # immutable kind: verification path only (D053 item 8)
    if hardness_of(c) == Hardness.HARD:
        if not (cfg.allow_hard or ignore_gates):
            return False
    else:
        if not (cfg.allow_soft or ignore_gates):
            return False
        if not (cfg.soft_default_relaxable or ref_of(c) in (overrides or {})):
            return False
    m = _mode_of(c)
    if m == "set":
        return cfg.location_enabled
    return m is not None


STATE_ATTRIBUTES = ("preferred_states",)


def is_state_rule(c: ConstraintT) -> bool:
    """D076: the supplier-state preference (a categorical set), the only categorical SOFT rule that can relax."""
    return (c.attribute in STATE_ATTRIBUTES and not c.is_doc_proof
            and (c.operator or "in").strip().lower() in ("in", "=", "==", "eq", "equals"))


def _state_set(c: ConstraintT) -> tuple[str, ...]:
    if isinstance(c, BuyerConstraint) and c.allowed_values is not None:
        return tuple(str(x).strip() for x in c.allowed_values if str(x).strip())
    v = _value_of(c)
    return tuple(x.strip() for x in _split_list(v)) if v else ()


def _mode_of(c: ConstraintT) -> Optional[str]:
    if is_state_rule(c):
        return "set"
    v = as_view(c)
    op = effective_op(v)
    if op in (">=", ">") and target_number(v) not in (None, 0):
        return "lower"
    if op in ("<=", "<") and target_number(v) not in (None, 0):
        return "upper"
    if op == "=" and target_number(v) not in (None, 0):
        return "band"
    if op == "range":
        r = parse_range(v.value)
        if r is not None and r[0] != 0 and r[1] != 0:
            return "range"
    return None


def _value_of(c: ConstraintT) -> Optional[str]:
    return c.buyer_value if isinstance(c, BuyerConstraint) else c.value


def _state_rule(c: ConstraintT, cfg: RelaxConfig, candidates: Sequence[CandidateView],
                o: Optional[Mapping]) -> Optional[RelaxRule]:
    pref = _state_set(c)
    if not pref:
        return None
    l1, l2 = widening_states(pref, [x.state for x in candidates], cfg.location_max_added)
    if not l1 and not l2:
        return None                                  # every pool state is already preferred: nothing to widen to
    s0 = tuple(pref)
    s1 = s0 + l1
    s2 = s1 + l2
    levels = (Fraction(0), Fraction(1, 2), Fraction(1))
    return RelaxRule(ref=ref_of(c), attribute=c.attribute, hardness=hardness_of(c), mode="set",
                     original=Fraction(0), original_hi=None, unit=None, original_value=str(_value_of(c)),
                     levels=levels, weight=_frac((o or {}).get("weight", cfg.location_cost_weight)),
                     category=cfg.location_category, level_sets=(s0, s1, s2), added=((), l1, l1 + l2))


def build_rules(constraints: Sequence[ConstraintT], cfg: RelaxConfig, *,
                roles: Optional[Mapping[str, ScenarioRole]] = None, ignore_gates: bool = False,
                overrides: Optional[Mapping[str, Mapping]] = None,
                candidates: Optional[Sequence[CandidateView]] = None) -> list[RelaxRule]:
    """Relaxable rules, sorted by ref.  Pass the ORIGINAL constraints (levels are always from the original).

    ``candidates`` (the pool) is needed for the D076 state rule only; without it that rule is not relaxable."""
    out: list[RelaxRule] = []
    for c in constraints:
        if not may_relax(c, cfg, roles, ignore_gates=ignore_gates, overrides=overrides):
            continue
        mode = _mode_of(c)
        assert mode is not None
        if mode == "set":
            sr = _state_rule(c, cfg, candidates, (overrides or {}).get(ref_of(c))) if candidates else None
            if sr is not None:
                out.append(sr)
            continue
        v = as_view(c)
        o = (overrides or {}).get(ref_of(c))
        levels = _rule_levels(cfg, o)
        if len(levels) < 2:
            continue                                 # nothing above level 0 -> not relaxable
        pn = parse_number_unit(v.value)
        unit = c.unit or (pn[1] if pn else None)
        if mode == "range":
            lo, hi = parse_range(v.value)  # type: ignore[misc]
            orig, orig_hi = Fraction(repr(float(lo))), Fraction(repr(float(hi)))
        else:
            orig, orig_hi = Fraction(repr(float(target_number(v)))), None  # type: ignore[arg-type]
        out.append(RelaxRule(ref=ref_of(c), attribute=c.attribute, hardness=hardness_of(c), mode=mode,
                             original=orig, original_hi=orig_hi, unit=unit, original_value=str(_value_of(c)),
                             levels=levels, weight=int((o or {}).get("weight", cfg.default_weight)),
                             category=c.constraint_category if isinstance(c, BuyerConstraint) else None))
    return sorted(out, key=lambda r: r.ref)


# ------------------------------------------------------------------ level arithmetic
def level_bounds(rule: RelaxRule, level: int) -> tuple[Fraction, Optional[Fraction]]:
    """(threshold_or_lo, hi_or_None) at ``level``, exact, from the ORIGINAL value (numeric rules only)."""
    if rule.mode == "set":
        raise ValueError("level_bounds is numeric-only; a set rule has level_sets")
    f = rule.levels[level]
    o = rule.original
    if rule.mode == "lower":
        return o - abs(o) * f, None
    if rule.mode == "upper":
        return o + abs(o) * f, None
    if rule.mode == "band":
        return o - abs(o) * f, o + abs(o) * f
    hi = rule.original_hi
    assert hi is not None
    return o - abs(o) * f, hi + abs(hi) * f


def _num(x: Fraction) -> str:
    return f"{float(x):g}"


def level_target(rule: RelaxRule, level: int) -> str:
    """Display value at ``level`` (what a Change record stores as ``new_value``).  Set rule: level 0 = the original
    value string, 1 = the widened set "A, B, C", top = "any state"."""
    if rule.mode == "set":
        if level == 0:
            return rule.original_value
        return "any state" if level == rule.top else ", ".join(rule.level_sets[level])
    a, b = level_bounds(rule, level)
    return _num(a) if b is None else f"{_num(a)}..{_num(b)}"


def level_percent(rule: RelaxRule, level: int) -> float:
    return float(rule.levels[level] * 100)


def relaxed_constraint(c: ConstraintT, rule: RelaxRule, level: int) -> ConstraintT:
    """Copy of the ORIGINAL constraint relaxed to ``level`` (level 0 returns ``c``)."""
    if level == 0:
        return c
    if rule.mode == "set":
        st = list(rule.level_sets[level])
        val = ", ".join(st)
        if isinstance(c, BuyerConstraint):
            return c.model_copy(update={"buyer_value": val, "allowed_values": st, "operator": "in"})
        assert isinstance(c, BelievedConstraint)
        return c.model_copy(update={"value": val, "operator": "in"})
    a, b = level_bounds(rule, level)
    if b is None:
        val = repr(float(a))
        op = c.operator
        if rule.mode == "lower" and (op or "=").strip() in ("=", "==", "eq", "equals"):
            op = ">="                                  # numeric "=" with allows_higher behaves as ">=" (D026)
        upd: dict = {"operator": op, "unit": rule.unit}
    else:
        val = f"{float(a)!r}..{float(b)!r}"
        upd = {"operator": "range", "unit": rule.unit}
    if isinstance(c, BuyerConstraint):
        upd.update(buyer_value=val, allows_higher=False if rule.mode == "lower" else c.allows_higher)
        return c.model_copy(update=upd)
    assert isinstance(c, BelievedConstraint)
    upd.update(value=val)
    return c.model_copy(update=upd)


def apply_levels(constraints: Sequence[ConstraintT], rules: Sequence[RelaxRule],
                 levels: Mapping[str, int]) -> list[ConstraintT]:
    """Constraints with each rule relaxed to ``levels[ref]`` (missing -> 0).  Originals are not modified."""
    by = {r.ref: r for r in rules}
    return [relaxed_constraint(c, by[ref_of(c)], int(levels.get(ref_of(c), 0))) if ref_of(c) in by else c
            for c in constraints]


# ------------------------------------------------------------------ D076 set-rule helpers
def added_states(rule: RelaxRule, level: int) -> tuple[str, ...]:
    """States a set rule adds at ``level`` versus the original set (what the negotiator proposes)."""
    return rule.added[level] if rule.mode == "set" else ()


def is_monotone(rule: RelaxRule) -> bool:
    """Set rules widen monotonically: every level's set contains the previous one and level 0 is untouched."""
    if rule.mode != "set":
        return True
    sets = [{norm_state(x) for x in lv} for lv in rule.level_sets]
    return all(a <= b for a, b in zip(sets, sets[1:]))


def state_level_for(rule: RelaxRule, to_value: Optional[str]) -> Optional[int]:
    """Smallest level of a set rule whose set covers ``to_value`` ("any state", or a list of states); None if
    ``to_value`` is not parsable or exceeds every level (numeric rules: None)."""
    if rule.mode != "set" or not to_value:
        return None
    t = to_value.strip().lower()
    if t in ("any state", "any", "anywhere", "all states"):
        return rule.top
    want = {norm_state(x) for x in _split_list(to_value)}
    if not want:
        return None
    for k, st in enumerate(rule.level_sets):
        if want <= {norm_state(x) for x in st}:
            return k
    return None
