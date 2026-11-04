"""D056 relaxation metrics: ``gain_estimate_error`` and ``belief_bounds_accuracy``.  Pure."""
from __future__ import annotations

from typing import Mapping, Optional, Sequence

from gtgen.contracts import GTConversation, RelaxationAnalysisRow, RelaxationOption


def gain_estimate_error(options: Sequence[RelaxationOption]) -> dict:
    """|negotiator_estimated_gain - truth_gain| over options that carry both.

    Returns n, mean_abs_error, max_abs_error, exact_share and mean_signed_error (estimate - truth; positive =
    the negotiator over-estimates).  Options missing either number are skipped (n counts only the evaluated ones)."""
    errs = [(o.negotiator_estimated_gain - o.truth_gain) for o in options
            if o.truth_gain is not None and o.negotiator_estimated_gain is not None]
    n = len(errs)
    return {"n": n,
            "mean_abs_error": (sum(abs(e) for e in errs) / n) if n else None,
            "max_abs_error": max((abs(e) for e in errs), default=None),
            "exact_share": (sum(1 for e in errs if e == 0) / n) if n else None,
            "mean_signed_error": (sum(errs) / n) if n else None}


def _ceiling(b: Optional[Mapping[str, int]]) -> Optional[int]:
    """Highest level a bound allows: fixed k -> k, max k -> k, no bound -> None (unbounded)."""
    if not b:
        return None
    return int(b["fixed"]) if "fixed" in b else int(b["max"]) if "max" in b else None


def bounds_match(truth: Optional[Mapping[str, int]], belief: Optional[Mapping[str, int]],
                 tolerance: float = 0.0) -> bool:
    """Same kind of bound (fixed vs max) with ceilings within ``tolerance`` levels; both unbounded matches."""
    ct, cb = _ceiling(truth), _ceiling(belief)
    if ct is None or cb is None:
        return ct is None and cb is None
    if ("fixed" in truth) != ("fixed" in belief):          # type: ignore[operator]
        return False
    return abs(ct - cb) <= tolerance


def belief_bounds_accuracy(rows: Sequence[RelaxationAnalysisRow], tolerance: float = 0.0, *,
                           ref_map: Optional[Mapping[str, str]] = None) -> dict:
    """Share of (turn, rule) bounds the negotiator's belief state reproduces.

    For every row, over the union of rules bounded in ``bounds_after_truth`` or in ``bounds_after_belief``
    (belief ids mapped to truth ids by ``ref_map``, identity by default) a rule is correct when ``bounds_match``
    holds within ``tolerance`` (settings relaxation.belief_bounds_tolerance).  Returns accuracy (None when no rule is
    bounded anywhere), n_rules, n_correct, and per_row accuracies."""
    ok = n = 0
    per_row: list[Optional[float]] = []
    for r in rows:
        belief = {(ref_map or {}).get(k, k): v for k, v in r.bounds_after_belief.items()}
        truth = dict(r.bounds_after_truth)
        keys = sorted(set(truth) | set(belief))
        rok = sum(1 for k in keys if bounds_match(truth.get(k), belief.get(k), tolerance))
        per_row.append(rok / len(keys) if keys else None)
        ok += rok
        n += len(keys)
    return {"accuracy": (ok / n) if n else None, "n_rules": n, "n_correct": ok, "per_row": per_row}


def conversation_ref_map(conv: GTConversation) -> dict[str, str]:
    """belief_id -> matched truth constraint_id over all negotiator belief states of a conversation."""
    out: dict[str, str] = {}
    for t in conv.turns:
        for b in t.negotiator_belief_state:
            if b.matched_constraint_id:
                out[b.belief_id] = b.matched_constraint_id
    return out
