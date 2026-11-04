"""D056 bounds model and loop state: buyer bounds per rule, ``apply_decision``, ``check_change`` (spec 12.2 items
1-8) and ``validate_transition`` (spec 12.3).  Pure; states are immutable (copy on write).

Bounds (spec 6): ``{"fixed": k}`` after YES (the rule sits at level k for good), ``{"max": k}`` after COUNTER at k,
``{"max": 0}`` after NO.  Bounds only tighten.  BYPASS and ACKNOWLEDGE change no bound (BYPASS only extends the
verification list).  The domain of a rule is the set of level indices still open.

Loop safety: ``RelaxState`` is all the front needs to be recomputed (``front.build_menu(..., state.bounds)``);
the ORIGINAL constraints are never modified, active values are derived from ``fixed`` bounds.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional, Sequence

from gtgen.contracts import Action
from gtgen.matching.rules import RelaxRule, is_monotone, level_target

Bounds = Mapping[str, Mapping[str, int]]


@dataclass(frozen=True)
class Change:
    """Append-only record of an accepted rule change (actor is always the buyer)."""
    ref: str
    original_value: str
    new_value: str
    level: int
    actor: str
    turn: int
    justification: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RelaxState:
    bounds: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    changes: tuple[Change, ...] = ()
    bypassed: tuple[str, ...] = ()
    diagnostic_used: bool = False


def domain_levels(rule: RelaxRule, bounds: Optional[Bounds]) -> list[int]:
    b = (bounds or {}).get(rule.ref)
    top = rule.top
    if not b:
        return list(range(top + 1))
    if "fixed" in b:
        return [min(int(b["fixed"]), top)]
    return list(range(min(int(b.get("max", top)), top) + 1))


def baseline_levels(rules: Sequence[RelaxRule], bounds: Optional[Bounds]) -> dict[str, int]:
    """Current active level per rule (lowest value of its domain)."""
    return {r.ref: domain_levels(r, bounds)[0] for r in rules}


def current_levels(rules: Sequence[RelaxRule], state: RelaxState) -> dict[str, int]:
    return baseline_levels(rules, state.bounds)


def tighten(bounds: Bounds, ref: str, *, fixed: Optional[int] = None, max_level: Optional[int] = None) -> dict:
    """Return a new bounds dict with ``ref`` tightened.  A fixed bound is never changed; a max never rises."""
    out = {k: dict(v) for k, v in bounds.items()}
    cur = out.get(ref)
    if cur and "fixed" in cur:
        return out
    if fixed is not None:
        out[ref] = {"fixed": fixed}
    elif max_level is not None:
        m = max_level if not cur else min(int(cur.get("max", max_level)), max_level)
        out[ref] = {"max": m}
    return out


# ------------------------------------------------------------------ decisions -> state
@dataclass(frozen=True)
class ItemOutcome:
    """Minimal view of one buyer item response used by the bounds model (buyer_decision.ItemDecision fits)."""
    ref: str
    action: Action
    level: int          # YES: accepted level; COUNTER: counter level k; others: proposed level


@dataclass(frozen=True)
class ApplyResult:
    state: RelaxState                 # the old state if ``errors`` is non-empty
    changes: tuple[Change, ...]       # changes appended by this transition
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def check_change(change: Change, rules: Mapping[str, RelaxRule], state_before: RelaxState, *,
                 proposal: Mapping[str, int], decision: Mapping[str, Action], diagnostic: bool = False,
                 not_permitted: Sequence[str] = ()) -> list[str]:
    """Spec 12.2 items 1-8 in gtgen terms.  ``rules`` holds exactly the relaxable (gate-permitted, negotiated,
    non-doc) rules, so item 1/2 = membership.  Item 8 (H3 conditions) does not exist in gtgen (D053 item 6)."""
    errs: list[str] = []
    rule = rules.get(change.ref)
    if rule is None:                                                       # 1, 2
        return [f"{change.ref}: not a relaxable rule"]
    if change.original_value != rule.original_value:                      # 3
        errs.append(f"{change.ref}: original_value mismatch")
    if change.actor != "buyer":                                            # 4
        errs.append(f"{change.ref}: actor must be 'buyer'")
    if not (0 < change.level <= rule.top):                                 # 5
        errs.append(f"{change.ref}: level {change.level} outside 1..{rule.top}")
    elif change.level not in domain_levels(rule, state_before.bounds):
        errs.append(f"{change.ref}: level {change.level} outside the open domain")
    elif change.new_value != level_target(rule, change.level):             # 6
        errs.append(f"{change.ref}: new_value does not match level {change.level}")
    if rule.mode == "set" and not is_monotone(rule):                       # D076: state sets only widen
        errs.append(f"{change.ref}: state set is not a monotone widening")
    if diagnostic or decision.get(change.ref) != Action.YES or proposal.get(change.ref) != change.level \
            or change.ref in not_permitted:                                # 7
        errs.append(f"{change.ref}: no buyer YES on a non-diagnostic proposal containing level {change.level}")
    return errs


def validate_transition(prev: RelaxState, new: RelaxState, rules: Mapping[str, RelaxRule]) -> list[str]:
    """Spec 12.3: changes only grow, no rule changes twice in one transition, every moved active value has a new
    change record, bounds: none lost, none loosened, no fixed bound changed."""
    errs: list[str] = []
    n = len(prev.changes)
    if new.changes[:n] != prev.changes:
        errs.append("accepted changes were rewritten or removed")
    added = new.changes[n:]
    refs = [c.ref for c in added]
    if len(refs) != len(set(refs)):
        errs.append("a rule changed twice in one transition")
    for ref, pb in prev.bounds.items():
        nb = new.bounds.get(ref)
        if nb is None:
            errs.append(f"{ref}: bound lost")
        elif "fixed" in pb and dict(nb) != dict(pb):
            errs.append(f"{ref}: fixed bound changed")
        elif "max" in pb:
            if "fixed" in nb:
                if nb["fixed"] > pb["max"]:
                    errs.append(f"{ref}: bound loosened")
            elif nb.get("max", 10 ** 9) > pb["max"]:
                errs.append(f"{ref}: bound loosened")
    for ref, rule in rules.items():                                   # D076: set rules widen monotonically
        if not is_monotone(rule):
            errs.append(f"{ref}: state set is not a monotone widening")
    old_lv = baseline_levels(list(rules.values()), prev.bounds)
    new_lv = baseline_levels(list(rules.values()), new.bounds)
    for ref, lv in new_lv.items():
        if lv != old_lv.get(ref, 0) and not any(c.ref == ref and c.level == lv for c in added):
            errs.append(f"{ref}: active value moved without a change record")
    return errs


def apply_decision(state: RelaxState, rules: Sequence[RelaxRule], proposal: Mapping[str, int],
                   items: Sequence[ItemOutcome], *, turn: int, diagnostic: bool = False,
                   justification: Optional[Mapping[str, Any]] = None,
                   not_permitted: Sequence[str] = ()) -> ApplyResult:
    """Apply the buyer's per-item decision to ``state`` (spec 12.1).

    YES -> ``{"fixed": level}`` + Change; NO -> ``{"max": 0}``; COUNTER -> ``{"max": k}``; BYPASS -> ref appended to
    ``bypassed`` only; ACKNOWLEDGE / diagnostic -> nothing (a diagnostic proposal sets ``diagnostic_used``).
    On any validation error the OLD state is returned with ``errors`` set."""
    by = {r.ref: r for r in rules}
    decision = {i.ref: i.action for i in items}
    if diagnostic:
        return ApplyResult(replace(state, diagnostic_used=True), ())
    bounds = {k: dict(v) for k, v in state.bounds.items()}
    changes: list[Change] = []
    bypassed = list(state.bypassed)
    for it in sorted(items, key=lambda i: i.ref):
        rule = by.get(it.ref)
        if it.action == Action.BYPASS:
            if it.ref not in bypassed:
                bypassed.append(it.ref)
            continue
        if it.action == Action.ACKNOWLEDGE or rule is None:
            continue
        if it.action == Action.YES:
            ch = Change(ref=it.ref, original_value=rule.original_value, new_value=level_target(rule, it.level),
                        level=it.level, actor="buyer", turn=turn,
                        justification={**(justification or {}), "decision": "YES", "proposal_turn": turn})
            errs = check_change(ch, by, state, proposal=proposal, decision=decision, not_permitted=not_permitted)
            if errs:
                return ApplyResult(state, (), tuple(errs))
            bounds = tighten(bounds, it.ref, fixed=it.level)
            changes.append(ch)
        elif it.action == Action.COUNTER:
            bounds = tighten(bounds, it.ref, max_level=it.level)
        elif it.action == Action.NO:
            bounds = tighten(bounds, it.ref, max_level=0)
    new = RelaxState(bounds=bounds, changes=state.changes + tuple(changes), bypassed=tuple(bypassed),
                     diagnostic_used=state.diagnostic_used)
    errs = validate_transition(state, new, by)
    if errs:
        return ApplyResult(state, (), tuple(errs))
    return ApplyResult(new, tuple(changes), ())
