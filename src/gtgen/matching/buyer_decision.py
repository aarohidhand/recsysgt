"""D053 buyer decision (spec 11.3) as PURE deterministic code.

``buyer_decide`` answers each item of a proposal (a menu point's changes) with YES / NO / COUNTER / BYPASS (or
ACKNOWLEDGE for a diagnostic proposal).  Randomness only through the caller-owned ``random.Random`` seeded with
``buyer_rng(seed)`` = ``Random(f"{seed}:buyer")``; one draw per item that reaches the probabilistic step, items in
sorted ref order, so a replay with the same rng state reproduces the decision.

Per item, in order (gtgen deviations of the spec are marked):
1. diagnostic proposal                      -> ACKNOWLEDGE (no bound change)
2. doc-proof item                           -> BYPASS (verification list only; never a relaxation, D053 item 8)
3. HARD item, or ref in ``not_permitted``   -> NO (reason ``policy``) -- CLAUDE.md rule 1.6
4. tolerance: level r > T_c (T_c = tolerance[ref] or tolerance[category], default 0, floor, capped at L_c):
   every tier and counter level k = T_c satisfies cur <= k < r -> COUNTER at k
   (per item, including every item of a multi-item proposal; a COUNTER is not an acceptance and does not
   consume the limit), else NO (``tolerance``)
5. limit: new distinct rules accepted (this proposal, in ref order) would exceed ``max_relaxations`` -> NO (``limit``)
6. probabilistic: p = sigmoid(alpha_eff*(softness - d) + beta*V - kappa), alpha_eff = alpha*(1 - decision_randomness),
   softness = tau_weight*(1 - H); tau_weight=1.5 (D082), d = r/L_c (this item), V = log(1+confirmed_recovery)/log(1+pool) clipped to [0,1]; YES if u < p else NO
   (``declined``).  H = persona.tau.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Collection, Mapping, Optional, Sequence

from gtgen.contracts import Action, Hardness, Persona, Tier
from gtgen.matching.bounds import ItemOutcome
from gtgen.matching.rules import RelaxConfig

def buyer_rng(seed: object) -> random.Random:
    return random.Random(f"{seed}:buyer")


@dataclass(frozen=True)
class BuyerParams:
    tau: float                                    # H, hardness
    tier: Tier
    max_relaxations: int
    decision_randomness: float
    tolerance: Mapping[str, float] = field(default_factory=dict)   # by constraint_id or constraint category
    alpha: float = 6.0
    beta: float = 2.0
    kappa: float = 0.0
    tau_weight: float = 1.5          # D082: amplifies (1-tau)'s pull on accept probability, independent of relaxation depth d
    patience_rounds: int = 3
    location_category: str = "LOCATION"           # D076
    location_strict_resistance: float = 1.0       # multiplier on a STRICT persona's location tolerance


def params_from_persona(persona: Persona, cfg: Optional[RelaxConfig] = None) -> BuyerParams:
    """Persona v3 fields; a missing one falls back to the midpoint of its settings range."""
    cfg = cfg or RelaxConfig()
    mid = lambda ab: (ab[0] + ab[1]) / 2  # noqa: E731
    return BuyerParams(
        tau=persona.tau, tier=persona.tier,
        max_relaxations=persona.max_relaxations if persona.max_relaxations is not None
        else int(round(mid(cfg.range_max_relaxations))),
        decision_randomness=persona.decision_randomness if persona.decision_randomness is not None
        else mid(cfg.range_decision_randomness),
        tolerance=dict(persona.tolerance), alpha=cfg.alpha, beta=cfg.beta, kappa=cfg.kappa, tau_weight=cfg.tau_weight,
        location_category=cfg.location_category, location_strict_resistance=cfg.location_strict_resistance,
        patience_rounds=persona.patience_rounds if persona.patience_rounds is not None
        else int(round(mid(cfg.range_patience_rounds))))


@dataclass(frozen=True)
class ProposalItem:
    ref: str
    level: int                      # proposed level r_c
    max_level: int                  # L_c
    hardness: Hardness = Hardness.SOFT
    category: Optional[str] = None
    doc_proof: bool = False
    current_level: int = 0          # level the rule already sits at


@dataclass(frozen=True)
class ItemDecision:
    ref: str
    action: Action
    proposed_level: int
    level: int                      # YES: proposed; COUNTER: counter k; else proposed
    reason: str = ""
    p_accept: Optional[float] = None
    u: Optional[float] = None

    def outcome(self) -> ItemOutcome:
        return ItemOutcome(self.ref, self.action, self.level)


@dataclass(frozen=True)
class BuyerDecision:
    items: tuple[ItemDecision, ...]
    diagnostic: bool = False

    def outcomes(self) -> list[ItemOutcome]:
        return [i.outcome() for i in self.items]

    def actions(self) -> dict[str, Action]:
        return {i.ref: i.action for i in self.items}


def v_signal(confirmed_recovery: int, pool: int) -> float:
    if pool <= 0 or confirmed_recovery <= 0:
        return 0.0
    return min(1.0, max(0.0, math.log(1 + confirmed_recovery) / math.log(1 + pool)))


def accept_probability(p: BuyerParams, level: int, top: int, v: float) -> float:
    d = level / top if top > 0 else 1.0
    a_eff = p.alpha * (1.0 - p.decision_randomness)
    softness = p.tau_weight * (1.0 - p.tau)            # D082: amplified tau pull
    z = a_eff * (softness - d) + p.beta * v - p.kappa
    return 1.0 / (1.0 + math.exp(-z)) if z > -700 else 0.0


_LOCATION_FALLBACK = ("SUPPLIER", "COMMERCIAL")


def location_tolerance(p: BuyerParams, ref: Optional[str] = None) -> float:
    """D076: the persona's OWN LOCATION tolerance; STRICT personas (tau >= 0.75) get it times
    ``relaxation.location.strict_resistance``.  Fallback while a persona has no LOCATION entry: its SUPPLIER (else
    COMMERCIAL) tolerance scaled by strict_resistance (any tier) -- a lenient budget tolerance must not make
    location free."""
    t = p.tolerance.get(ref) if ref is not None else None
    if t is None:
        t = p.tolerance.get(p.location_category)
    if t is not None:
        return t * (p.location_strict_resistance if p.tier == Tier.STRICT else 1.0)
    for k in _LOCATION_FALLBACK:
        if k in p.tolerance:
            return p.tolerance[k] * p.location_strict_resistance
    return 0.0


def _tolerance(p: BuyerParams, it: ProposalItem) -> int:
    if it.category is not None and it.category == p.location_category:
        return min(int(math.floor(location_tolerance(p, it.ref) or 0.0)), it.max_level)
    t = p.tolerance.get(it.ref)
    if t is None and it.category is not None:
        t = p.tolerance.get(it.category)
    return min(int(math.floor(t or 0.0)), it.max_level)


def buyer_decide(items: Sequence[ProposalItem], params: BuyerParams, rng: random.Random, *,
                 accepted_rules: Collection[str] = (), confirmed_recovery: int = 0, pool: int = 0,
                 diagnostic: bool = False, not_permitted: Collection[str] = ()) -> BuyerDecision:
    """See the module docstring.  ``accepted_rules`` = refs already accepted (fixed) in earlier turns."""
    if diagnostic:
        return BuyerDecision(tuple(ItemDecision(i.ref, Action.ACKNOWLEDGE, i.level, i.level, "diagnostic")
                                   for i in sorted(items, key=lambda x: x.ref)), True)
    v = v_signal(confirmed_recovery, pool)
    taken = set(accepted_rules)
    out: list[ItemDecision] = []
    for it in sorted(items, key=lambda x: x.ref):
        r = it.level
        if it.doc_proof:
            out.append(ItemDecision(it.ref, Action.BYPASS, r, r, "verification"))
            continue
        if it.hardness == Hardness.HARD or it.ref in not_permitted:
            out.append(ItemDecision(it.ref, Action.NO, r, r, "policy"))
            continue
        # A COUNTER is not an acceptance: it does not consume max_relaxations.  Check tolerance first so a
        # multi-item proposal gets a counter for every item, even when accepting them all would exceed the
        # persona's relaxation limit.  The limit is applied below, only to an actual YES.
        tol = _tolerance(params, it)
        if r > tol:
            if it.current_level <= tol < r:
                out.append(ItemDecision(it.ref, Action.COUNTER, r, tol, "tolerance"))
            else:
                out.append(ItemDecision(it.ref, Action.NO, r, r, "tolerance"))
            continue
        if it.ref not in taken and len(taken) + 1 > params.max_relaxations:
            out.append(ItemDecision(it.ref, Action.NO, r, r, "limit"))
            continue
        p = accept_probability(params, r, it.max_level, v)
        u = rng.random()
        if u < p:
            out.append(ItemDecision(it.ref, Action.YES, r, r, "accepted", p, u))
            taken.add(it.ref)
        else:
            out.append(ItemDecision(it.ref, Action.NO, r, r, "declined", p, u))
    return BuyerDecision(tuple(out))
