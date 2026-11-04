"""Relaxation search: single options, near-miss bundles (D025), numeric "=" bands (D026).

Pure and deterministic. Only SOFT (truth) / believed-SOFT (belief) constraints are ever relaxed;
a candidate with any HARD FAIL or HARD UNKNOWN is never counted as gained.

Evidence is needed to know *what value* a candidate would need, so the search takes the same
``alignments`` / ``normalised`` inputs as ``compliance.compliance_matrix``; statuses come from ``cells``.
Relaxed constraints are re-evaluated with ``evidence.evaluate`` (recomputed, never assumed).

Complexity of ``search_bundles``: let N = near-miss candidates, U = distinct blocker sets, m = max_size.
Subsets are the closure of the blocker sets under union restricted to size <= m (a subset that is not
such a union contains an item no covered candidate needs and is non-minimal anyway), at most
sum_{k<=m} C(|blockers|, k) (hard cap ``MAX_SUBSETS``).  Each subset costs O(N * m) evaluations and
the minimality check O(m * N * m).  Order of everything is sorted -> fully deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

from gtgen.contracts import (
    AttributeAlignment,
    CandidateView,
    Compliance,
    ComplianceCell,
    Hardness,
    NormalisedValue,
    RelaxationBundle,
    RelaxationOption,
)
from gtgen.matching.compliance import cells_by_product, feasible, is_inactive, synthetic_normalised
from gtgen.matching.evidence import (
    ConstraintT,
    ConstraintView,
    Evidence,
    as_view,
    buyer_set,
    effective_op,
    evaluate,
    extract_evidence,
    fmt,
    hardness_of,
    parse_number_unit,
    ref_of,
    _range_of,
    target_number,
    token,
    close,
)
from gtgen.matching.units import convert

MAX_SUBSETS = 5000


@dataclass(frozen=True)
class RelaxParams:
    categorical_extend_cost: float = 0.5
    drop_cost: float = 1.0
    eq_rel_tol: float = 1.0e-6
    max_blockers: int = 4


def params_from_settings(settings: Optional[dict]) -> RelaxParams:
    bs = ((settings or {}).get("negotiation", {}) or {}).get("bundle_search", {}) or {}
    d = RelaxParams()
    return RelaxParams(
        categorical_extend_cost=float(bs.get("categorical_extend_cost", d.categorical_extend_cost)),
        drop_cost=float(bs.get("drop_cost", d.drop_cost)),
        eq_rel_tol=float(bs.get("numeric_eq_rel_tolerance", d.eq_rel_tol)),
        max_blockers=int(bs.get("max_blockers", d.max_blockers)),
    )


# ------------------------------------------------------------------ needs and levels
@dataclass(frozen=True)
class Need:
    """What one failing candidate requires of one constraint."""
    kind: str          # num_lower | num_upper | num_band | num_range | cat_add | cat_remove | doc | drop
    a: float = 0.0     # lower: ev.lo | upper: ev.hi | band: d | range: ev.lo
    b: float = 0.0     # range: ev.hi
    tokens: tuple[str, ...] = ()
    shown: tuple[str, ...] = ()


@dataclass(frozen=True)
class Level:
    ref: str
    kind: str                       # RelaxationOption kind
    view: Optional[ConstraintView]  # relaxed view (None for drop / doc)
    from_value: Optional[str]
    to_value: Optional[str]
    cost: float
    step: Optional[float] = None    # scalar used by nearest_gain_value


def _need(view: ConstraintView, ev: Evidence) -> Need:
    if view.is_doc_proof:
        return Need("doc")
    op = effective_op(view)
    t = target_number(view)
    if view.rng is not None:
        return Need("drop")
    if op in (">=", ">") and t is not None and ev.lo is not None:
        return Need("num_lower", a=ev.lo)
    if op in ("<=", "<") and t is not None and ev.hi is not None:
        return Need("num_upper", a=ev.hi)
    if op == "=" and t is not None and ev.lo is not None:
        return Need("num_band", a=max(abs(ev.lo - t), abs(ev.hi - t)))
    if op == "range" and _range_of(view) is not None and ev.lo is not None:
        return Need("num_range", a=ev.lo, b=ev.hi)
    bs = buyer_set(view)
    if op in ("in", "=") and bs is not None and ev.values:
        pairs = sorted((tok, d) for tok, d in zip(ev.values, ev.display) if tok not in bs)
        if pairs:
            return Need("cat_add", tokens=(pairs[0][0],), shown=(pairs[0][1],))
    if op in ("not_in", "!=") and bs is not None and ev.values and t is None:
        hit = tuple(sorted(set(ev.values) & set(bs)))
        if hit:
            shown = {tok: d for tok, d in zip(ev.values, ev.display)}
            return Need("cat_remove", tokens=hit, shown=tuple(shown.get(h, h) for h in hit))
    return Need("drop")


def _cap(x: float) -> float:
    return round(min(max(x, 0.0), 1.0), 12)


def _tunit(view: ConstraintView) -> Optional[str]:
    if view.unit:
        return view.unit
    p = parse_number_unit(view.value)
    return p[1] if p else None


def make_level(view: ConstraintView, needs: Sequence[Need], p: RelaxParams) -> Level:
    """Loosest level satisfying every need (all of the same kind, else drop)."""
    kinds = {n.kind for n in needs}
    ref = view.ref
    if len(kinds) != 1 or "drop" in kinds:
        return Level(ref, "drop", None, view.value, None, _cap(p.drop_cost))
    kind = next(iter(kinds))
    if kind == "doc":
        return Level(ref, "doc_to_pending", None, view.doc_name or view.attribute, "pending verification", 0.0)
    unit = _tunit(view)
    t = target_number(view)
    if kind in ("num_lower", "num_upper"):
        assert t is not None
        bound = min(n.a for n in needs) if kind == "num_lower" else max(n.a for n in needs)
        nv = ConstraintView(**{**view.__dict__, "operator": ">=" if kind == "num_lower" else "<=",
                               "value": repr(float(bound)), "unit": unit, "allows_higher": False})
        cost = 1.0 if t == 0 else abs(t - bound) / abs(t)
        return Level(ref, "numeric_widen", nv, fmt(t), fmt(bound), _cap(cost), step=bound)
    if kind == "num_band":
        assert t is not None
        d = max(n.a for n in needs)
        lo, hi = t - d, t + d
        nv = ConstraintView(**{**view.__dict__, "operator": "range", "rng": (lo, hi), "unit": unit,
                               "allows_higher": False})
        cost = 1.0 if t == 0 else d / abs(t)
        return Level(ref, "numeric_widen", nv, fmt(t), f"{fmt(lo)}..{fmt(hi)}", _cap(cost), step=d)
    if kind == "num_range":
        rg = _range_of(view)
        assert rg is not None
        lo, hi = min(rg[0], min(n.a for n in needs)), max(rg[1], max(n.b for n in needs))
        nv = ConstraintView(**{**view.__dict__, "operator": "range", "rng": (lo, hi), "unit": unit})
        costs = []
        if lo < rg[0]:
            costs.append(1.0 if rg[0] == 0 else (rg[0] - lo) / abs(rg[0]))
        if hi > rg[1]:
            costs.append(1.0 if rg[1] == 0 else (hi - rg[1]) / abs(rg[1]))
        return Level(ref, "numeric_widen", nv, f"{fmt(rg[0])}..{fmt(rg[1])}", f"{fmt(lo)}..{fmt(hi)}",
                     _cap(max(costs) if costs else 0.0))
    bs = buyer_set(view) or ()
    shown_map = {tok: sh for n in needs for tok, sh in zip(n.tokens, n.shown)}
    if kind == "cat_add":
        new = tuple(sorted(set(bs) | {tok for n in needs for tok in n.tokens}))
        nv = ConstraintView(**{**view.__dict__, "operator": "in", "allowed": new, "allowed_unparsed": False})
        return Level(ref, "categorical_extend", nv, ", ".join(sorted(bs)), ", ".join(new),
                     _cap(p.categorical_extend_cost))
    if kind == "cat_remove":
        rem = {tok for n in needs for tok in n.tokens}
        new = tuple(sorted(set(bs) - rem))
        nv = ConstraintView(**{**view.__dict__, "operator": "not_in", "allowed": new, "allowed_unparsed": False})
        return Level(ref, "categorical_extend", nv, ", ".join(sorted(bs)), ", ".join(new),
                     _cap(p.categorical_extend_cost))
    return Level(ref, "drop", None, view.value, None, _cap(p.drop_cost))


# ------------------------------------------------------------------ context
class _Ctx:
    def __init__(self, constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView],
                 cells: Iterable[ComplianceCell], alignments: Optional[Mapping[str, AttributeAlignment]],
                 normalised: Optional[Mapping[tuple[str, str], NormalisedValue]], p: RelaxParams,
                 quantity: Optional[float] = None):
        self.p = p
        constraints = [c for c in constraints if not is_inactive(c)]
        self.views = {ref_of(c): as_view(c) for c in constraints}
        self.hardness: dict[str, Hardness] = {ref_of(c): hardness_of(c) for c in constraints}
        self.cands = {c.product_id: c for c in candidates}
        self.by = cells_by_product(cells)
        self.alignments = alignments or {}
        self.normalised = dict(normalised or {})
        # D049: synthetic commercial terms take their evidence from catalogue fields (needs ``quantity``)
        self.normalised.update(synthetic_normalised(constraints, candidates, quantity))
        self._ev: dict[tuple[str, str], Evidence] = {}
        self.base_feasible: dict[str, bool] = {}
        self.hard_block: dict[str, bool] = {}
        self.soft_fail: dict[str, frozenset[str]] = {}
        for pid, d in self.by.items():
            self.base_feasible[pid] = feasible(d.values(), self.hardness)
            hb = False
            sf = []
            for ref, cell in d.items():
                if not cell.scored:      # D048: unscored cells never block
                    continue
                h = self.hardness.get(ref)
                if h == Hardness.HARD and cell.status in (Compliance.FAIL, Compliance.UNKNOWN):
                    hb = True
                if h == Hardness.SOFT and cell.status == Compliance.FAIL:
                    sf.append(ref)
            self.hard_block[pid] = hb
            self.soft_fail[pid] = frozenset(sf)
        self.n_feasible = sum(1 for v in self.base_feasible.values() if v)

    def ev(self, pid: str, ref: str) -> Evidence:
        k = (pid, ref)
        if k not in self._ev:
            v = self.views[ref]
            cand = self.cands.get(pid)
            if cand is None:
                self._ev[k] = Evidence()
            else:
                self._ev[k] = extract_evidence(v, cand, self.alignments.get(v.attribute),
                                               self.normalised.get((pid, v.attribute)))
        return self._ev[k]

    def need(self, pid: str, ref: str) -> Need:
        return _need(self.views[ref], self.ev(pid, ref))

    def blocked_pool(self, max_blockers: Optional[int] = None) -> list[str]:
        """Infeasible, no HARD block, at least one SOFT FAIL (optionally <= max_blockers)."""
        out = []
        for pid in sorted(self.by):
            if self.base_feasible[pid] or self.hard_block[pid]:
                continue
            n = len(self.soft_fail[pid])
            if n == 0 or (max_blockers is not None and n > max_blockers):
                continue
            out.append(pid)
        return out

    def passes_under(self, level: Level, pid: str) -> bool:
        """True if the candidate's cell on level.ref is not FAIL after relaxing."""
        if level.kind == "drop":
            return True
        if level.kind == "doc_to_pending":
            return True  # FAIL/UNKNOWN -> PENDING (non-blocking)
        assert level.view is not None
        return evaluate(level.view, self.ev(pid, level.ref), self.p.eq_rel_tol) != Compliance.FAIL

    def gained(self, levels: Mapping[str, Level], pool: Iterable[str]) -> list[str]:
        out = []
        for pid in pool:
            sf = self.soft_fail[pid]
            if sf and sf <= set(levels) and all(self.passes_under(levels[r], pid) for r in sf):
                out.append(pid)
        return sorted(out)

    def option(self, level: Level, pool: Iterable[str]) -> RelaxationOption:
        g = self.gained({level.ref: level}, pool)
        return RelaxationOption(constraint_ref=level.ref, kind=level.kind, from_value=level.from_value,
                                to_value=level.to_value, normalised_cost=level.cost,
                                candidates_gained=len(g), feasible_after=self.n_feasible + len(g),
                                gained_product_ids=g)


def _soft_refs(ctx: _Ctx) -> list[str]:
    return sorted(r for r, h in ctx.hardness.items() if h == Hardness.SOFT)


# ------------------------------------------------------------------ refused options (D045)
Refused = Mapping[str, Sequence[RelaxationOption]]


def _parse_pair(a: str) -> Optional[tuple[float, Optional[str]]]:
    return parse_number_unit(a)


def _numeric_width(o: RelaxationOption) -> Optional[tuple[float, Optional[str]]]:
    """(width of the relaxation, unit) or None if it cannot be determined.

    One-sided: |to - from|.  Band "lo..hi" from a point: half-width (hi-lo)/2.  Range "lo..hi" widened
    from a range "flo..fhi": half of the total widening ((hi-lo)-(fhi-flo))/2."""
    to, frm = o.to_value, o.from_value
    if to is None or frm is None:
        return None
    if ".." in to:
        lo_s, _, hi_s = to.partition("..")
        lo, hi = _parse_pair(lo_s), _parse_pair(hi_s)
        if lo is None or hi is None:
            return None
        unit = lo[1] or hi[1]
        span = hi[0] - lo[0]
        if ".." in frm:
            fl_s, _, fh_s = frm.partition("..")
            fl, fh = _parse_pair(fl_s), _parse_pair(fh_s)
            if fl is None or fh is None:
                return None
            span -= fh[0] - fl[0]
            unit = unit or fl[1] or fh[1]
        return span / 2.0, unit
    pt, pf = _parse_pair(to), _parse_pair(frm)
    if pt is None or pf is None:
        return None
    if pt[1] and pf[1]:
        v = convert(pt[0], pt[1], pf[1])
        if v is None:
            return None
        return abs(v - pf[0]), pf[1]
    return abs(pt[0] - pf[0]), pt[1] or pf[1]


def _cat_delta(o: RelaxationOption) -> Optional[frozenset[str]]:
    """Set of values added (extend) or removed (not_in) by the option, or None if not determinable."""
    if o.to_value is None or o.from_value is None:
        return None
    a = {token(x) for x in o.from_value.split(", ") if x.strip()}
    b = {token(x) for x in o.to_value.split(", ") if x.strip()}
    d = frozenset(a ^ b)
    return d or None


def is_equal_or_looser(candidate: RelaxationOption, refused: RelaxationOption) -> bool:
    """True if ``candidate`` is equal to or looser than the ``refused`` option of the same constraint.

    Rules (anything else, including incomparable data, returns False -> never over-exclude):
    - different ``constraint_ref`` -> False.
    - refused ``drop``: True for candidate kinds drop / numeric_widen / categorical_extend
      (drop is looser than every widen/extend); a candidate doc_to_pending is a different dimension -> False.
    - candidate ``drop``: True if refused is drop, numeric_widen or categorical_extend; False if refused is
      doc_to_pending.
    - ``doc_to_pending``: only excludes another doc_to_pending.
    - ``numeric_widen`` vs ``numeric_widen``: compare widths of the relaxation from the original bound,
      candidate_width >= refused_width (within eq tolerance).  Width = |to - from| (one-sided), half-width
      (hi-lo)/2 for the D026 band "lo..hi", half of the total widening for a widened range.  Units are
      converted with the module's unit table; unknown/incompatible unit pair or unparsable value -> False.
    - ``categorical_extend`` vs ``categorical_extend``: the set of values the candidate adds (or removes, for
      not_in) is a superset of, or equal to, the refused set; empty/missing sets -> False.
    - numeric vs categorical (kind mismatch) -> False.
    """
    if candidate.constraint_ref != refused.constraint_ref:
        return False
    ck, rk = candidate.kind, refused.kind
    if ck == "doc_to_pending" or rk == "doc_to_pending":
        return ck == rk
    if ck == "drop":
        return True
    if rk == "drop":
        return True
    if ck != rk:
        return False
    if ck == "numeric_widen":
        wc, wr = _numeric_width(candidate), _numeric_width(refused)
        if wc is None or wr is None:
            return False
        w_r = wr[0]
        if wc[1] and wr[1] and wc[1] != wr[1]:
            c = convert(w_r, wr[1], wc[1])
            if c is None:
                return False
            w_r = c
        return wc[0] >= w_r or close(wc[0], w_r)
    if ck == "categorical_extend":
        dc, dr = _cat_delta(candidate), _cat_delta(refused)
        if dc is None or dr is None:
            return False
        return dr <= dc
    return False


def _excluded(opt: RelaxationOption, refused: Optional[Refused]) -> bool:
    if not refused:
        return False
    return any(is_equal_or_looser(opt, r) for r in refused.get(opt.constraint_ref, ()))


def _level_as_option(level: "Level") -> RelaxationOption:
    return RelaxationOption(constraint_ref=level.ref, kind=level.kind, from_value=level.from_value,
                            to_value=level.to_value, normalised_cost=level.cost, candidates_gained=0,
                            feasible_after=0)


def _level_excluded(level: "Level", refused: Optional[Refused]) -> bool:
    return bool(refused) and level.ref in refused and _excluded(_level_as_option(level), refused)


# ------------------------------------------------------------------ single-option search
def _levels_for(ctx: _Ctx, ref: str, pool: Sequence[str]) -> list[Level]:
    """All distinct single-step levels for one constraint from the failing candidates' needs."""
    needs: dict[Need, None] = {}
    for pid in pool:
        if ref in ctx.soft_fail[pid]:
            needs[ctx.need(pid, ref)] = None
    view = ctx.views[ref]
    levels: dict[tuple, Level] = {}
    kinds = {n.kind for n in needs}
    if not needs:
        return []
    if kinds & {"doc"}:
        lv = make_level(view, [n for n in needs if n.kind == "doc"], ctx.p)
        return [lv]
    if kinds == {"drop"} or "drop" in kinds and len(kinds) == 1:
        return [make_level(view, [Need("drop")], ctx.p)]
    for n in sorted(needs, key=lambda x: (x.kind, x.a, x.b, x.tokens)):
        if n.kind == "drop":
            continue
        lv = make_level(view, [n], ctx.p)
        levels[(lv.kind, lv.to_value)] = lv
    return list(levels.values())


def _sort_options(opts: list[RelaxationOption]) -> list[RelaxationOption]:
    return sorted(opts, key=lambda o: (-o.candidates_gained, o.normalised_cost, o.constraint_ref,
                                       o.to_value or ""))


def search(constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView],
           cells: Iterable[ComplianceCell], alignments: Optional[Mapping[str, AttributeAlignment]] = None,
           normalised: Optional[Mapping[tuple[str, str], NormalisedValue]] = None, *,
           params: Optional[RelaxParams] = None, include_zero_gain: bool = False,
           refused: Optional[Refused] = None, quantity: Optional[float] = None) -> list[RelaxationOption]:
    """Single relaxation options, sorted by candidates_gained desc, normalised_cost asc, constraint_ref.

    One option per distinct step (numeric: each distinct failing candidate value beyond the bound;
    categorical: each candidate value outside the allowed set; doc-proof: one doc_to_pending; other
    operators: one drop).  Zero-gain options are omitted unless ``include_zero_gain``.
    Levels equal to or looser than a ``refused`` option of the same constraint are skipped (D045).
    """
    ctx = _Ctx(constraints, candidates, cells, alignments, normalised, params or RelaxParams(), quantity)
    pool = ctx.blocked_pool()
    out: list[RelaxationOption] = []
    for ref in _soft_refs(ctx):
        for lv in _levels_for(ctx, ref, pool):
            if _level_excluded(lv, refused):
                continue
            o = ctx.option(lv, pool)
            if o.candidates_gained > 0 or include_zero_gain:
                out.append(o)
    return _sort_options(out)


# ------------------------------------------------------------------ bundle search (D025)
def _subsets(blocker_sets: Sequence[frozenset[str]], max_size: int) -> list[frozenset[str]]:
    seen: set[frozenset[str]] = {b for b in blocker_sets if len(b) <= max_size}
    frontier = set(seen)
    while frontier and len(seen) < MAX_SUBSETS:
        new: set[frozenset[str]] = set()
        for a in sorted(frontier, key=lambda s: tuple(sorted(s))):
            for b in blocker_sets:
                u = a | b
                if len(u) <= max_size and u not in seen and u not in new:
                    new.add(u)
        seen |= new
        frontier = new
    return sorted(seen, key=lambda s: (len(s), tuple(sorted(s))))


def search_bundles(constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView],
                   cells: Iterable[ComplianceCell], max_size: int = 4, min_marginal_gain: int = 1,
                   alignments: Optional[Mapping[str, AttributeAlignment]] = None,
                   normalised: Optional[Mapping[tuple[str, str], NormalisedValue]] = None, *,
                   params: Optional[RelaxParams] = None,
                   refused: Optional[Refused] = None, quantity: Optional[float] = None) -> list[RelaxationBundle]:
    """Near-miss bundle search.  Ranked: joint_gain desc, fewer items, lower total cost, sorted refs.

    Near-miss = infeasible only because of 1..max_size SOFT FAILs (any HARD FAIL/UNKNOWN excludes).
    Each item is relaxed to the loosest level needed by near-miss candidates whose blocker set is a
    subset of the bundle.  Joint gain is recomputed with all items applied.  A bundle is kept only if
    removing any single item lowers joint gain by at least ``min_marginal_gain``.

    ``refused`` (D045): for a ref with refused options, near-miss candidates whose need on that ref is
    equal to or looser than a refused option are not covered through that ref; the level is built from the
    remaining needs (none left, or the resulting level itself excluded -> no bundle for that subset).
    Joint gain and minimality are computed on these restricted levels.
    """
    ctx = _Ctx(constraints, candidates, cells, alignments, normalised, params or RelaxParams(), quantity)
    near = ctx.blocked_pool(min(max_size, ctx.p.max_blockers) if ctx.p.max_blockers else max_size)
    near = [pid for pid in near if len(ctx.soft_fail[pid]) <= max_size]
    if not near:
        return []
    subsets = _subsets(sorted({ctx.soft_fail[p] for p in near}, key=lambda s: tuple(sorted(s))), max_size)
    results: list[RelaxationBundle] = []
    for S in subsets:
        covered = [p for p in near if ctx.soft_fail[p] <= S]
        levels: dict[str, Level] = {}
        for ref in sorted(S):
            needs = [ctx.need(p, ref) for p in covered if ref in ctx.soft_fail[p]]
            if refused and refused.get(ref):
                needs = [n for n in needs
                         if not _level_excluded(make_level(ctx.views[ref], [n], ctx.p), refused)]
            if not needs:
                break
            lv = make_level(ctx.views[ref], needs, ctx.p)
            if _level_excluded(lv, refused):
                break
            levels[ref] = lv
        else:
            joint = ctx.gained(levels, near)
            if len(joint) < min_marginal_gain:
                continue
            minimal = True
            for ref in sorted(S):
                rest = {r: l for r, l in levels.items() if r != ref}
                if len(joint) - len(ctx.gained(rest, near)) < min_marginal_gain:
                    minimal = False
                    break
            if not minimal:
                continue
            items = []
            for ref in sorted(S):
                o = ctx.option(levels[ref], near)
                items.append(o)
            results.append(RelaxationBundle(
                items=items, joint_gain=len(joint), feasible_after=ctx.n_feasible + len(joint),
                gained_product_ids=joint,
                total_normalised_cost=round(sum(i.normalised_cost for i in items), 12)))
    results.sort(key=lambda b: (-b.joint_gain, len(b.items), b.total_normalised_cost,
                                tuple(sorted(i.constraint_ref for i in b.items))))
    return results


def select_bundle(constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView],
                  cells: Iterable[ComplianceCell], max_size: int = 4, min_marginal_gain: int = 1,
                  alignments: Optional[Mapping[str, AttributeAlignment]] = None,
                  normalised: Optional[Mapping[tuple[str, str], NormalisedValue]] = None, *,
                  params: Optional[RelaxParams] = None,
                  refused: Optional[Refused] = None, quantity: Optional[float] = None) -> Optional[RelaxationBundle]:
    """Top-ranked bundle from ``search_bundles`` or None."""
    r = search_bundles(constraints, candidates, cells, max_size, min_marginal_gain, alignments,
                       normalised, params=params, refused=refused, quantity=quantity)
    return r[0] if r else None


# ------------------------------------------------------------------ diagnostics
_BLOCKING = {(Hardness.HARD, Compliance.FAIL), (Hardness.HARD, Compliance.UNKNOWN),
             (Hardness.SOFT, Compliance.FAIL)}


def blockers(product_id: str, constraints: Sequence[ConstraintT], cells: Iterable[ComplianceCell]) -> list[str]:
    """Refs blocking feasibility for one candidate, HARD first, then input order."""
    cmap = {r: c for r, c in cells_by_product(cells).get(product_id, {}).items() if c.scored}
    ordered = sorted(constraints, key=lambda c: 0 if hardness_of(c) == Hardness.HARD else 1)
    return [ref_of(c) for c in ordered
            if ref_of(c) in cmap and (hardness_of(c), cmap[ref_of(c)].status) in _BLOCKING]


def first_failing(product_id: str, constraints: Sequence[ConstraintT],
                  cells: Iterable[ComplianceCell]) -> Optional[str]:
    """First blocking constraint ref (HARD FAIL/UNKNOWN before SOFT FAIL, then input order) or None."""
    b = blockers(product_id, constraints, cells)
    return b[0] if b else None


@dataclass
class Bottleneck:
    fail_count: int = 0           # candidates with FAIL on this constraint
    first_failing_count: int = 0  # candidates whose first blocker is this constraint
    sole_blocker_count: int = 0   # infeasible candidates for which this is the only blocker


def bottleneck_counts(constraints: Sequence[ConstraintT], cells: Iterable[ComplianceCell]) -> dict[str, Bottleneck]:
    cells = list(cells)
    out = {ref_of(c): Bottleneck() for c in constraints}
    for cell in cells:
        if cell.status == Compliance.FAIL and cell.scored and cell.constraint_ref in out:
            out[cell.constraint_ref].fail_count += 1
    by = cells_by_product(cells)
    for pid in sorted(by):
        bl = blockers(pid, constraints, by[pid].values())
        if bl:
            out[bl[0]].first_failing_count += 1
            if len(bl) == 1:
                out[bl[0]].sole_blocker_count += 1
    return out


def nearest_gain_value(constraint: ConstraintT, buyer_counter_value: str,
                       constraints: Sequence[ConstraintT], candidates: Sequence[CandidateView],
                       cells: Iterable[ComplianceCell],
                       alignments: Optional[Mapping[str, AttributeAlignment]] = None,
                       normalised: Optional[Mapping[tuple[str, str], NormalisedValue]] = None, *,
                       required_gain: int = 1, params: Optional[RelaxParams] = None,
                       refused: Optional[Refused] = None, quantity: Optional[float] = None) -> Optional[RelaxationOption]:
    """Negotiator's counter-counter: the single-constraint step nearest to the buyer's counter that
    still gains >= ``required_gain`` candidates (negotiation.negotiator_counter_requires_gain).

    Numeric: nearest distinct candidate value (band: nearest half-width) to the counter; categorical:
    the counter itself if it gains, else the cheapest gaining value.  Returns the option (its
    ``to_value`` is the value to propose) or None.
    """
    ctx = _Ctx(constraints, candidates, cells, alignments, normalised, params or RelaxParams(), quantity)
    ref = ref_of(constraint)
    if ctx.hardness.get(ref) != Hardness.SOFT:
        return None
    pool = ctx.blocked_pool()
    view = ctx.views[ref]
    cn = parse_number_unit(buyer_counter_value)
    t = target_number(view)
    best: Optional[tuple[tuple, RelaxationOption]] = None
    for lv in _levels_for(ctx, ref, pool):
        if _level_excluded(lv, refused):
            continue
        o = ctx.option(lv, pool)
        if o.candidates_gained < required_gain:
            continue
        if lv.step is not None and cn is not None:
            ref_pt = abs(cn[0] - t) if (lv.view and lv.view.rng is not None and t is not None
                                       and view.operator == "=" and not view.allows_higher) else cn[0]
            dist = abs(lv.step - ref_pt)
        elif lv.kind == "categorical_extend":
            dist = 0.0 if (lv.to_value and buyer_counter_value.strip().lower() in lv.to_value.split(", ")) else 1.0
        else:
            dist = 0.0
        key = (dist, lv.cost, o.to_value or "")
        if best is None or key < best[0]:
            best = (key, o)
    return best[1] if best else None
