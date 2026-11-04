"""Ranking and calibration metrics.  Pure functions on ids / probabilities.

Ranked lists are lists of product_ids, best first.  ``relevant`` is a set of ids (binary relevance);
``gains`` maps id -> graded gain for NDCG.
"""
from __future__ import annotations

import math
from typing import Mapping, Optional, Sequence


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if k <= 0:
        return 0.0
    return sum(1 for x in ranked[:k] if x in relevant) / k


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return sum(1 for x in ranked[:k] if x in relevant) / len(relevant)


def reciprocal_rank(ranked: Sequence[str], relevant: set[str]) -> float:
    for i, x in enumerate(ranked, start=1):
        if x in relevant:
            return 1.0 / i
    return 0.0


def mrr(rankeds: Sequence[Sequence[str]], relevants: Sequence[set[str]]) -> float:
    """Mean reciprocal rank over queries (conversations)."""
    if not rankeds:
        return 0.0
    return sum(reciprocal_rank(r, rel) for r, rel in zip(rankeds, relevants)) / len(rankeds)


def average_precision(ranked: Sequence[str], relevant: set[str]) -> float:
    """AP = (1/|R|) * sum over relevant hits of precision at that rank."""
    if not relevant:
        return 0.0
    hits, acc = 0, 0.0
    for i, x in enumerate(ranked, start=1):
        if x in relevant:
            hits += 1
            acc += hits / i
    return acc / len(relevant)


def mean_average_precision(rankeds: Sequence[Sequence[str]], relevants: Sequence[set[str]]) -> float:
    if not rankeds:
        return 0.0
    return sum(average_precision(r, rel) for r, rel in zip(rankeds, relevants)) / len(rankeds)


def dcg_at_k(ranked: Sequence[str], gains: Mapping[str, float], k: int) -> float:
    return sum(gains.get(x, 0.0) / math.log2(i + 1) for i, x in enumerate(ranked[:k], start=1))


def ndcg_at_k(ranked: Sequence[str], gains: Mapping[str, float], k: int) -> float:
    """DCG with gain/log2(i+1); ideal = gains sorted descending (over all items in ``gains``)."""
    ideal = sorted((g for g in gains.values() if g > 0), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 1) for i, g in enumerate(ideal, start=1))
    return dcg_at_k(ranked, gains, k) / idcg if idcg > 0 else 0.0


def kendall_tau(ranked_a: Sequence[str], ranked_b: Sequence[str]) -> Optional[float]:
    """Kendall tau-a over the items common to both lists, positions from list order (no ties).
    None if fewer than 2 common items."""
    pos_b = {x: i for i, x in enumerate(ranked_b)}
    common = [x for x in ranked_a if x in pos_b]
    n = len(common)
    if n < 2:
        return None
    conc = disc = 0
    for i in range(n):
        for j in range(i + 1, n):
            # common is in a-order, so a-order says i before j
            if pos_b[common[i]] < pos_b[common[j]]:
                conc += 1
            else:
                disc += 1
    return (conc - disc) / (n * (n - 1) / 2)


def brier_score(probs: Sequence[float], outcomes: Sequence[bool]) -> float:
    if not probs:
        return 0.0
    return sum((p - float(o)) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def expected_calibration_error(probs: Sequence[float], outcomes: Sequence[bool], n_bins: int = 10) -> float:
    """ECE with equal-width bins on [0,1] (p == 1 falls in the last bin)."""
    n = len(probs)
    if n == 0:
        return 0.0
    bins: list[list[tuple[float, float]]] = [[] for _ in range(n_bins)]
    for p, o in zip(probs, outcomes):
        b = min(int(p * n_bins), n_bins - 1)
        bins[b].append((p, float(o)))
    ece = 0.0
    for b in bins:
        if b:
            conf = sum(p for p, _ in b) / len(b)
            acc = sum(o for _, o in b) / len(b)
            ece += len(b) / n * abs(conf - acc)
    return ece


def rate(num: int, den: int) -> Optional[float]:
    return None if den == 0 else num / den


def share_in_band(values: Sequence[float], band: tuple[float, float]) -> Optional[float]:
    """Share of ``values`` v with band[0] <= v <= band[1] (both ends inclusive, 1e-12 slack); None if empty.
    The band is an argument: nothing here reads recommendation.tier_targets."""
    if not values:
        return None
    lo, hi = band
    return sum(1 for v in values if lo - 1e-12 <= v <= hi + 1e-12) / len(values)


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (numpy default) of an ascending list; q in [0, 1]."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("quantile of an empty list")
    pos = q * (n - 1)
    i = int(math.floor(pos))
    j = min(i + 1, n - 1)
    return sorted_values[i] + (sorted_values[j] - sorted_values[i]) * (pos - i)


def distribution(values: Sequence[float]) -> dict:
    """{n, mean, min, p25, median, p75, max}; stats are None when n == 0."""
    xs = sorted(values)
    if not xs:
        return {"n": 0, "mean": None, "min": None, "p25": None, "median": None, "p75": None, "max": None}
    return {"n": len(xs), "mean": sum(xs) / len(xs), "min": xs[0], "p25": quantile(xs, 0.25),
            "median": quantile(xs, 0.5), "p75": quantile(xs, 0.75), "max": xs[-1]}
