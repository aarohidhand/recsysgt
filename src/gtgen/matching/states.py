"""D076 data: Indian state / union-territory adjacency (land borders, plus contiguous UT enclaves) and the
deterministic "which states does level 1 add" rule.  Pure; no I/O.

``norm_state`` maps a free-text state to a canonical lower-case key (aliases resolved).  ``widening_states``
returns the states a set-widening relaxation adds at level 1 and level 2, ranked by the number of POOL candidates
located there (catalogue-visible), ties by name.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Mapping, Optional, Sequence

_EDGES: dict[str, tuple[str, ...]] = {
    "andhra pradesh": ("telangana", "karnataka", "tamil nadu", "odisha", "chhattisgarh", "puducherry"),
    "arunachal pradesh": ("assam", "nagaland"),
    "assam": ("arunachal pradesh", "nagaland", "manipur", "mizoram", "tripura", "meghalaya", "west bengal"),
    "bihar": ("uttar pradesh", "jharkhand", "west bengal"),
    "chandigarh": ("punjab", "haryana"),
    "chhattisgarh": ("madhya pradesh", "maharashtra", "telangana", "andhra pradesh", "odisha", "jharkhand",
                     "uttar pradesh"),
    "dadra and nagar haveli and daman and diu": ("gujarat", "maharashtra"),
    "delhi": ("haryana", "uttar pradesh"),
    "goa": ("maharashtra", "karnataka"),
    "gujarat": ("rajasthan", "madhya pradesh", "maharashtra", "dadra and nagar haveli and daman and diu"),
    "haryana": ("punjab", "himachal pradesh", "uttarakhand", "uttar pradesh", "rajasthan", "delhi", "chandigarh"),
    "himachal pradesh": ("jammu and kashmir", "ladakh", "punjab", "haryana", "uttarakhand", "uttar pradesh",
                         "chandigarh"),
    "jammu and kashmir": ("punjab", "himachal pradesh", "ladakh"),
    "jharkhand": ("bihar", "west bengal", "odisha", "chhattisgarh", "uttar pradesh"),
    "karnataka": ("maharashtra", "goa", "kerala", "tamil nadu", "andhra pradesh", "telangana"),
    "kerala": ("karnataka", "tamil nadu", "puducherry"),
    "ladakh": ("jammu and kashmir", "himachal pradesh"),
    "madhya pradesh": ("uttar pradesh", "chhattisgarh", "maharashtra", "gujarat", "rajasthan"),
    "maharashtra": ("gujarat", "madhya pradesh", "chhattisgarh", "telangana", "karnataka", "goa",
                    "dadra and nagar haveli and daman and diu"),
    "manipur": ("nagaland", "mizoram", "assam"),
    "meghalaya": ("assam",),
    "mizoram": ("assam", "manipur", "tripura"),
    "nagaland": ("arunachal pradesh", "assam", "manipur"),
    "odisha": ("west bengal", "jharkhand", "chhattisgarh", "andhra pradesh"),
    "puducherry": ("tamil nadu", "kerala", "andhra pradesh"),
    "punjab": ("jammu and kashmir", "himachal pradesh", "haryana", "rajasthan", "chandigarh"),
    "rajasthan": ("punjab", "haryana", "uttar pradesh", "madhya pradesh", "gujarat", "delhi"),
    "sikkim": ("west bengal",),
    "tamil nadu": ("kerala", "karnataka", "andhra pradesh", "puducherry"),
    "telangana": ("maharashtra", "karnataka", "andhra pradesh", "chhattisgarh"),
    "tripura": ("assam", "mizoram"),
    "uttar pradesh": ("uttarakhand", "himachal pradesh", "haryana", "delhi", "rajasthan", "madhya pradesh",
                      "chhattisgarh", "jharkhand", "bihar"),
    "uttarakhand": ("himachal pradesh", "haryana", "uttar pradesh"),
    "west bengal": ("bihar", "jharkhand", "odisha", "assam", "sikkim"),
    "andaman and nicobar islands": (),
    "lakshadweep": (),
}

_ALIASES = {
    "orissa": "odisha", "uttaranchal": "uttarakhand", "pondicherry": "puducherry", "new delhi": "delhi",
    "nct of delhi": "delhi", "national capital territory of delhi": "delhi", "delhi ncr": "delhi",
    "jammu & kashmir": "jammu and kashmir", "j&k": "jammu and kashmir", "daman and diu": "dadra and nagar haveli and daman and diu",
    "dadra and nagar haveli": "dadra and nagar haveli and daman and diu",
    "dadra & nagar haveli": "dadra and nagar haveli and daman and diu",
    "andaman & nicobar islands": "andaman and nicobar islands", "andaman and nicobar": "andaman and nicobar islands",
    "chattisgarh": "chhattisgarh", "telengana": "telangana", "tamilnadu": "tamil nadu", "tamil nadu ": "tamil nadu",
}


def norm_state(s: object) -> str:
    t = re.sub(r"\s+", " ", str(s).strip().lower().replace("&", " and "))
    t = re.sub(r"\s+", " ", t).strip()
    return _ALIASES.get(t, t)


def _build() -> dict[str, frozenset[str]]:
    adj: dict[str, set[str]] = {k: set() for k in _EDGES}
    for a, bs in _EDGES.items():
        for b in bs:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)          # symmetric
    return {k: frozenset(v) for k, v in adj.items()}


ADJACENCY: Mapping[str, frozenset[str]] = _build()


def adjacent(state: object) -> frozenset[str]:
    return ADJACENCY.get(norm_state(state), frozenset())


def pool_state_counts(states: Iterable[Optional[str]]) -> dict[str, tuple[int, str]]:
    """normalised key -> (candidate count, display spelling = most frequent raw spelling, ties by spelling)."""
    raw: dict[str, Counter] = {}
    for s in states:
        if s is None or not str(s).strip():
            continue
        raw.setdefault(norm_state(s), Counter())[str(s).strip()] += 1
    return {k: (sum(c.values()), sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]) for k, c in raw.items()}


def widening_states(preferred: Sequence[str], pool_states: Iterable[Optional[str]],
                    max_added: int = 2) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(level1_added, level2_added) display names, both disjoint from ``preferred``.

    level1 = up to ``max_added`` states adjacent to any preferred state that have >= 1 pool candidate, ranked by
    pool candidate count desc then name; if none is adjacent-with-candidates, the most common pool states outside
    the preferred set.  level2 = every other pool state (together with level 1: drop the preference = any state).
    """
    counts = pool_state_counts(pool_states)
    pref = {norm_state(p) for p in preferred}
    outside = {k: v for k, v in counts.items() if k not in pref}
    nbrs = set()
    for p in pref:
        nbrs |= adjacent(p)
    key = lambda k: (-outside[k][0], outside[k][1].lower(), k)  # noqa: E731
    cand = sorted((k for k in outside if k in nbrs), key=key) or sorted(outside, key=key)
    l1 = tuple(outside[k][1] for k in cand[:max(0, max_added)])
    l1_keys = {norm_state(x) for x in l1}
    l2 = tuple(outside[k][1] for k in sorted((k for k in outside if k not in l1_keys), key=key))
    return l1, l2
