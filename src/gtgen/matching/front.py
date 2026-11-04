"""D053 need tables, exact joint Pareto front, menu, diagnostic front (spec 7, 8, 10).  Pure, deterministic.

gtgen translation (D053 amendment): "products/suppliers" = candidates/suppliers; the rules that decide coverage are
the relaxable SOFT negotiated rules (HARD ones only under ``gates.allow_hard_relaxation``); verification_only rules
never enter a need table or front.  A candidate is *dead* (relaxation cannot save it) when it is infeasible through a
non-relaxable rule (``compliance.feasible`` over the cells of every other active rule).

Need tables: ``need[p][c]`` = smallest level index in the rule's open domain at which candidate ``p`` is not blocked by
rule ``c``.  SOFT rule: blocked = FAIL (UNKNOWN does not block, exactly like ``compliance.feasible``); HARD rule
(gate on): strict = PASS/PENDING, hopeful additionally UNKNOWN.  Domains honour the buyer bounds (``bounds.py``), so a
fixed rule has one level and a refused/pinned rule never offers a useful value above 0.

Front: Cartesian product of useful values (lowest domain value + every need value), exact; more than
``max_combinations`` combinations raises ``FrontTooLarge`` (no silent fallback).  Points changing more than
``max_items`` rules are dropped; the staircase keeps a point only if it recovers strictly more suppliers than every
cheaper point (ties: more products, then the lexicographically smallest level tuple).  The counts reported on menu
points come from ``gain.role_aware_gain`` (the one counting path); ``tests`` assert they equal the need-table counts.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Mapping, Optional, Sequence

from gtgen.contracts import Compliance, Hardness, ScenarioRole
from gtgen.matching.bounds import Bounds, baseline_levels, domain_levels
from gtgen.matching.compliance import hardness_map, feasible as _feasible, cells_by_product, is_inactive
from gtgen.matching.evidence import ConstraintT, ref_of
from gtgen.matching.gain import EvalContext, GainResult, role_aware_gain
from gtgen.matching.rules import (
    RelaxConfig,
    RelaxRule,
    added_states,
    build_rules,
    level_percent,
    level_target,
    relaxed_constraint,
)
from gtgen.contracts import CandidateView


class FrontTooLarge(RuntimeError):
    pass


@dataclass(frozen=True)
class NeedTables:
    strict: Mapping[str, Mapping[str, Optional[int]]]
    hopeful: Mapping[str, Mapping[str, Optional[int]]]
    dead: frozenset[str]
    supplier_of: Mapping[str, str]
    refs: tuple[str, ...]


@dataclass(frozen=True)
class FrontPoint:
    levels: tuple[tuple[str, int], ...]       # (ref, level index) for EVERY rule of the front, sorted by ref
    cost: Fraction
    strict_suppliers: frozenset[str]
    strict_products: frozenset[str]
    hopeful_suppliers: frozenset[str]
    hopeful_products: frozenset[str]
    diagnostic: bool = False

    def level_map(self) -> dict[str, int]:
        return dict(self.levels)


def _passes(rule: RelaxRule, status: Compliance, scored: bool) -> tuple[bool, bool]:
    """(strict pass, hopeful pass) for one cell."""
    if not scored:
        return True, True
    if rule.hardness == Hardness.SOFT:
        ok = status != Compliance.FAIL
        return ok, ok
    return (status in (Compliance.PASS, Compliance.PENDING_VERIFICATION),
            status != Compliance.FAIL)


def need_tables(rules: Sequence[RelaxRule], constraints: Sequence[ConstraintT],
                candidates: Sequence[CandidateView], ctx: EvalContext,
                bounds: Optional[Bounds] = None) -> NeedTables:
    by_ref = {ref_of(c): c for c in constraints}
    rule_refs = {r.ref for r in rules}
    other = [c for c in constraints if not is_inactive(c) and ref_of(c) not in rule_refs]
    dead: set[str] = set()
    if other:
        hm = hardness_map(other)
        for pid, d in cells_by_product(ctx.cells(other, candidates)).items():
            if not _feasible(d.values(), hm):
                dead.add(pid)
    strict: dict[str, dict[str, Optional[int]]] = {c.product_id: {} for c in candidates}
    hopeful: dict[str, dict[str, Optional[int]]] = {c.product_id: {} for c in candidates}
    for r in rules:
        c0 = by_ref[r.ref]
        for pid in strict:
            strict[pid][r.ref] = None
            hopeful[pid][r.ref] = None
        for lv in domain_levels(r, bounds):
            cells = {x.product_id: x for x in ctx.cells([relaxed_constraint(c0, r, lv)], candidates)}
            for pid in strict:
                cell = cells.get(pid)
                s, h = _passes(r, cell.status, cell.scored) if cell is not None else (False, False)
                if s and strict[pid][r.ref] is None:
                    strict[pid][r.ref] = lv
                if h and hopeful[pid][r.ref] is None:
                    hopeful[pid][r.ref] = lv
    return NeedTables(strict=strict, hopeful=hopeful, dead=frozenset(dead),
                      supplier_of={c.product_id: c.supplier_id for c in candidates},
                      refs=tuple(sorted(rule_refs)))


def reachable_suppliers(rules: Sequence[RelaxRule], tables: NeedTables,
                        bounds: Optional[Bounds] = None) -> frozenset[str]:
    """Suppliers with a live product whose HOPEFUL need exists for every rule within its domain (k_eff input)."""
    out = set()
    for pid, d in tables.hopeful.items():
        if pid in tables.dead:
            continue
        if all(d[r.ref] is not None for r in rules):
            out.add(tables.supplier_of[pid])
    return frozenset(out)


def cost_of(rules: Sequence[RelaxRule], levels: Mapping[str, int]) -> Fraction:
    return sum((Fraction(r.weight * levels.get(r.ref, 0), r.top) for r in rules), Fraction(0))


def _covered(rules: Sequence[RelaxRule], table: Mapping[str, Mapping[str, Optional[int]]], dead: frozenset[str],
             levels: Mapping[str, int]) -> frozenset[str]:
    out = set()
    for pid, d in table.items():
        if pid in dead:
            continue
        if all(d[r.ref] is not None and levels[r.ref] >= d[r.ref] for r in rules):  # type: ignore[operator]
            out.add(pid)
    return frozenset(out)


def front(rules: Sequence[RelaxRule], tables: NeedTables, bounds: Optional[Bounds] = None, *,
          max_combinations: int = 5000, max_items: int = 4, diagnostic: bool = False) -> list[FrontPoint]:
    """Exact joint Pareto front (staircase of strictly greater recovered-supplier count)."""
    rules = sorted(rules, key=lambda r: r.ref)
    if not rules:
        return []
    base = baseline_levels(rules, bounds)
    useful: list[list[int]] = []
    for r in rules:
        dom = domain_levels(r, bounds)
        vals = {dom[0]}
        for t in (tables.strict, tables.hopeful):
            vals |= {d[r.ref] for pid, d in t.items() if pid not in tables.dead and d[r.ref] is not None}
        useful.append(sorted(v for v in vals if v in dom))
    size = 1
    for u in useful:
        size *= len(u)
    if size > max_combinations:
        raise FrontTooLarge(f"{size} level combinations exceed max_relaxation_combinations={max_combinations}")
    base_prod = _covered(rules, tables.strict, tables.dead, base)
    base_sup = {tables.supplier_of[p] for p in base_prod}
    cand_points = []
    for combo in itertools.product(*useful):
        lv = {r.ref: v for r, v in zip(rules, combo)}
        if sum(1 for r in rules if lv[r.ref] > base[r.ref]) > max_items:
            continue
        prod = _covered(rules, tables.strict, tables.dead, lv)
        sup = {tables.supplier_of[p] for p in prod}
        rec_p, rec_s = prod - base_prod, sup - base_sup
        if not rec_p or not rec_s:
            continue
        cost = cost_of(rules, lv)
        cand_points.append(((cost, -len(rec_s), -len(rec_p), combo), lv, rec_p, rec_s))
    cand_points.sort(key=lambda t: t[0])
    out: list[FrontPoint] = []
    best = 0
    for key, lv, rec_p, rec_s in cand_points:
        if len(rec_s) <= best:
            continue
        best = len(rec_s)
        hp = _covered(rules, tables.hopeful, tables.dead, lv)
        hbase = _covered(rules, tables.hopeful, tables.dead, base)
        hp_rec = hp - hbase
        out.append(FrontPoint(
            levels=tuple((r.ref, lv[r.ref]) for r in rules), cost=key[0],
            strict_suppliers=frozenset(rec_s), strict_products=frozenset(rec_p),
            hopeful_suppliers=frozenset(tables.supplier_of[p] for p in hp_rec), hopeful_products=frozenset(hp_rec),
            diagnostic=diagnostic))
    return out


# ------------------------------------------------------------------ menu
@dataclass(frozen=True)
class MenuPoint:
    index: int
    point: FrontPoint
    changes: tuple[dict, ...]            # one dict per rule raised above its current level
    gain: GainResult                     # role_aware_gain from the current levels to the point
    verified_before: int                 # in ``unit``
    verified_after: int
    cost: float
    is_reference: bool
    diagnostic: bool = False

    def proposal(self) -> dict[str, int]:
        """ref -> proposed level for the changed rules only."""
        return {c["ref"]: c["level"] for c in self.changes}

    def to_dict(self) -> dict:
        """JSON-able record (RelaxationAnalysisRow.front_points entries)."""
        return {"index": self.index, "levels": {c["ref"]: c["level"] for c in self.changes}, "cost": self.cost,
                "supplier_count": len(self.point.strict_suppliers), "product_count": len(self.point.strict_products),
                "gain_products": self.gain.gain_products, "gain_suppliers": self.gain.gain_suppliers,
                "verified_after": self.verified_after, "reference": self.is_reference,
                "diagnostic": self.diagnostic}


@dataclass(frozen=True)
class Menu:
    points: tuple[MenuPoint, ...]
    reference_index: Optional[int]
    diagnostic: bool = False

    def reference(self) -> Optional[MenuPoint]:
        return None if self.reference_index is None else self.points[self.reference_index]


def _changes(rules: Mapping[str, RelaxRule], point: FrontPoint, base: Mapping[str, int]) -> tuple[dict, ...]:
    out = []
    for ref, lv in point.levels:
        if lv > base[ref]:
            r = rules[ref]
            d = {"ref": ref, "attribute": r.attribute, "original_value": r.original_value,
                 "current_value": level_target(r, base[ref]), "new_value": level_target(r, lv),
                 "level": lv, "max_level": r.top, "level_fraction": str(r.levels[lv]),
                 "percent": level_percent(r, lv)}
            if r.mode == "set":          # D076: state widening
                d.update(kind="state_widen", added_states=list(added_states(r, lv)),
                         current_added_states=list(added_states(r, base[ref])), any_state=lv == r.top)
            out.append(d)
    return tuple(out)


def build_menu(constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView], ctx: EvalContext,
               cfg: RelaxConfig, bounds: Optional[Bounds] = None, *, unit: str = "supplier",
               diagnostic_used: bool = False,
               overrides: Optional[Mapping[str, Mapping]] = None) -> Menu:
    """Loop-safe: recomputes rules' domains, need tables and the joint front from the CURRENT ``bounds``.

    Authorized points (gate-permitted rules) with gain > 0 in ``unit``, at most ``cfg.menu_size`` (cheapest first).
    The first is the reference choice.  If there is none and ``diagnostic_used`` is False, the diagnostic front
    (gates ignored) is returned instead, at most ``cfg.max_diagnostic_points`` points, marked ``diagnostic`` (the
    buyer can only acknowledge them; never applied)."""
    pts = _menu_for(constraints, candidates, ctx, cfg, bounds, unit, False, overrides, cfg.menu_size)
    if pts or diagnostic_used:
        return Menu(tuple(pts), 0 if pts else None, False)
    dpts = _menu_for(constraints, candidates, ctx, cfg, bounds, unit, True, overrides, cfg.max_diagnostic_points)
    return Menu(tuple(dpts), 0 if dpts else None, bool(dpts))


def _menu_for(constraints, candidates, ctx, cfg, bounds, unit, diagnostic, overrides, limit) -> list[MenuPoint]:
    rules = build_rules(constraints, cfg, roles=ctx.roles, ignore_gates=diagnostic, overrides=overrides,
                        candidates=candidates)
    if not rules:
        return []
    by = {r.ref: r for r in rules}
    tables = need_tables(rules, constraints, candidates, ctx, bounds)
    fp = front(rules, tables, bounds, max_combinations=cfg.max_combinations, max_items=cfg.max_items,
               diagnostic=diagnostic)
    base = baseline_levels(rules, bounds)
    out: list[MenuPoint] = []
    for p in fp:
        ch = _changes(by, p, base)
        if not ch:
            continue
        g = role_aware_gain(constraints, candidates, ctx, rules=rules, levels_before=base, levels_after=p.level_map())
        if g.gain(unit) <= 0:
            continue
        out.append(MenuPoint(index=len(out), point=p, changes=ch, gain=g, verified_before=g.count_before(unit),
                             verified_after=g.count_after(unit), cost=float(p.cost), is_reference=not out,
                             diagnostic=diagnostic))
        if len(out) >= limit:
            break
    return out


def agrees_with_reference(menu: Menu, chosen_index: Optional[int]) -> Optional[bool]:
    """True iff the negotiator chose the reference (cheapest) point; None if there is nothing to choose."""

    if menu.reference_index is None or chosen_index is None:
        return None
    return chosen_index == menu.reference_index



def second_point(points: Sequence[MenuPoint], first: MenuPoint, max_items: int) -> Optional[MenuPoint]:
    """Cheapest OTHER point whose changed rules are disjoint from ``first`` and that together fit ``max_items``."""
    first_refs = {c["ref"] for c in first.changes}
    for p in points:
        if p.index == first.index:
            continue
        refs = {c["ref"] for c in p.changes}
        if refs & first_refs:
            continue
        if len(first_refs | refs) > max_items:
            continue
        return p
    return None

def diagnostic_front(constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView], ctx: EvalContext,
                     cfg: RelaxConfig, bounds: Optional[Bounds] = None, *, unit: str = "supplier",
                     overrides: Optional[Mapping[str, Mapping]] = None) -> list[MenuPoint]:
    return _menu_for(constraints, candidates, ctx, cfg, bounds, unit, True, overrides, cfg.max_diagnostic_points)