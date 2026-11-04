"""r6/209245: 'verified feasible' (the RECOMMEND gate).

A candidate that is merely not blocked is not a recommendation: when the unscored (verification_only / unknown) cells
carry the whole conversation, 'feasible' only means 'nothing contradicts it'.  Verified feasible =

* feasible (no scored HARD FAIL/UNKNOWN, no scored SOFT FAIL; rule 6 unchanged) AND
* at least one scored PASS / PENDING_VERIFICATION on a negotiated *specification* belief.  Budget / MOQ and supplier
  location are commercial or ranking terms every candidate can pass without any product fact being confirmed, so
  they do not count as verification.

If the buyer stated no specification belief at all (only commercial terms), nothing needs verifying and every feasible
candidate counts.  If specification beliefs exist but every one is verification_only (or has no scored PASS), nothing is
verified.

D050 interaction: the negotiator still ALWAYS ends with an offer.  RECOMMENDED requires >= min_shortlist verified
candidates; otherwise the final turn is BEST_AVAILABLE, with reason ``nothing_verified`` when the plain feasible count
reached the shortlist (so relaxation cannot help: the gap is evidence, not a blocker) and the usual reasons otherwise.
"""
from __future__ import annotations

import re
from typing import Mapping, Optional, Sequence

from gtgen.contracts import BelievedConstraint, ComplianceCell, ScenarioRole
from gtgen.matching.compliance import is_spec_constraint, verified_feasible_ids


def is_spec_belief(b: BelievedConstraint) -> bool:
    return is_spec_constraint(b)


def verified_feasible(feasible: Sequence[str], beliefs: Sequence[BelievedConstraint], cells: Sequence[ComplianceCell],
                      roles: Optional[Mapping[str, ScenarioRole]]) -> list[str]:
    """Belief-side call into the generic D075/D081 gate (``gtgen.matching.compliance.verified_feasible_ids``),
    kept here (same name/signature) because ``negotiator.py`` and tests import it from this module."""
    return verified_feasible_ids(feasible, beliefs, cells, roles)


def dedupe_to_confirm(facts: dict) -> dict:
    """r6: an open item is listed ONCE.  When every option carries the same ``to_confirm`` list it is sent at the top
    level only (merged with the top-level list, order kept, no repeats); otherwise nothing changes except that
    the top-level list loses repeats."""
    opts = [dict(o) for o in facts.get("options") or []]
    top = [x for x in dict.fromkeys(facts.get("to_confirm") or [])]
    lists = [tuple(o.get("to_confirm") or []) for o in opts]
    if opts and len(set(lists)) == 1 and lists[0]:
        for o in opts:
            o["to_confirm"] = []
        top = list(dict.fromkeys(list(lists[0]) + top))
    return {**facts, "options": opts, "to_confirm": top}


_SQMM_RE = re.compile(r"^(?:sq\.?\s*mm|sqmm|mm\s*(?:2|\u00b2)|square\s*mm|sq\.?\s*millimet(?:er|re)s?)$")


def is_unit_only_value(value: Optional[str]) -> bool:
    """r6: a value that is just a unit token ('Sqmm', 'kW', 'mm') with no number.  A single letter or a count word
    ('A', 'no') may be a real categorical value and is never treated as a unit here."""
    from gtgen.matching.units import _TABLE, norm_unit

    s = (value or "").strip().lower().rstrip(".")
    if not s or any(ch.isdigit() for ch in s):
        return False
    if _SQMM_RE.match(s):
        return True
    u = norm_unit(s)
    return bool(u) and len(u) >= 2 and u in _TABLE and _TABLE[u][0] != "count"
