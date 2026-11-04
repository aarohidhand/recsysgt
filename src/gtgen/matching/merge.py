"""D046 (narrowed): merge value-less clause-conformity members citing the same standard into one doc-proof constraint.

Pure and deterministic.  Member detection (all must hold):
* provenance "gem", framing_decision not DROP / MERGE, ``merged_from`` empty (a merged constraint is never a member);
* cites a standard identifier (``IS|ISO|IEC|BIS|EN|ASTM|DIN|BS`` upper-case + 3..6 digits) in ``raw_text``,
  ``buyer_value`` or ``commercial_text``; with several ids the one shared by most candidate constraints wins
  (ties by key), so groups partition;
* NO measurable value: the operator is not numeric (>=, <=, >, <, range) and, after removing the standard
  identifier (with a ``:YYYY`` year suffix) from ``buyer_value``, no number-with-unit remains.
Groups are keyed by (standard, hardness) and merged only with >= ``scenario.merge_min_members`` members, so a
SOFT member can never become HARD.  No-op when ``scenario.merge_clause_conformity`` is false.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Mapping, Optional, Sequence

from gtgen.contracts import BuyerConstraint, FramingDecision
from gtgen.matching.evidence import NUMERIC_OPS, norm_op, parse_number_unit

_STD_RE = re.compile(r"(?<![A-Za-z])(IS|ISO|IEC|BIS|EN|ASTM|DIN|BS)(?![A-Za-z])[\s:/\-]*(?:NO\.?\s*)?(\d{3,6})"
                     r"(?:\s*[:/\-]\s*(?:19|20)\d\d)?")
_NUM_WITH_UNIT = re.compile(r"[-+]?\d[\d,]*\.?\d*\s*[A-Za-z%°/²³]+")


def _std_keys(text: Optional[str]) -> set[str]:
    return {f"{m.group(1)} {m.group(2)}" for m in _STD_RE.finditer(text)} if text else set()


def _has_measurable_value(c: BuyerConstraint) -> bool:
    if norm_op(c.operator) in NUMERIC_OPS:
        return True
    rest = _STD_RE.sub(" ", c.buyer_value or "").strip()
    if not rest:
        return False
    p = parse_number_unit(rest)
    if p is not None and p[1] is not None:
        return True
    return any((q := parse_number_unit(m.group(0).strip())) is not None and q[1] is not None
               for m in _NUM_WITH_UNIT.finditer(rest))


def _candidate(c: BuyerConstraint) -> bool:
    return (c.provenance == "gem" and c.framing_decision not in (FramingDecision.DROP, FramingDecision.MERGE)
            and not c.merged_from)


def _keys_of(c: BuyerConstraint) -> set[str]:
    return _std_keys(c.raw_text) | _std_keys(c.buyer_value) | _std_keys(c.commercial_text)


def _unique(xs):
    return list(dict.fromkeys(xs))


def _merged(std: str, members: Sequence[BuyerConstraint]) -> BuyerConstraint:
    first = members[0]
    hardness = first.hardness
    cats = Counter(m.constraint_category for m in members if m.constraint_category)
    cat = sorted(cats.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if cats else "DOCUMENT"
    digest = hashlib.sha1(f"{std}|{hardness.value}".encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "_", std.lower()).strip("_")
    return BuyerConstraint(
        constraint_id=f"{first.bid_id}_M_{digest}", bid_id=first.bid_id, golden_param=first.golden_param,
        hardness=hardness, provenance="gem", attribute=f"conformity_{slug}", spec_name="Conformity",
        buyer_value=std, operator="required", constraint_category=cat, constraint_type="certification",
        scope="product", is_doc_proof=True, doc_name=f"{std} conformity certificate",
        framing_decision=FramingDecision.KEEP,
        framing_reason=f"merged {len(members)} clause-conformity members citing {std}",
        commercial_text=f"The supplier provides the {std} conformity certificate.",
        merged_from=sorted(m.constraint_id for m in members),
        llm_call_ids=_unique(i for m in members for i in m.llm_call_ids))


def merge_clause_conformity(constraints: Sequence[BuyerConstraint], settings: Mapping) -> list[BuyerConstraint]:
    sc = (settings or {}).get("scenario", {}) or {}
    if not sc.get("merge_clause_conformity", False):
        return list(constraints)
    min_members = int(sc.get("merge_min_members", 2))
    cands = [c for c in constraints if _candidate(c) and not _has_measurable_value(c) and _keys_of(c)]
    support: Counter = Counter(k for c in cands for k in _keys_of(c))
    groups: dict[tuple[str, str], list[BuyerConstraint]] = {}
    for c in cands:
        std = sorted(_keys_of(c), key=lambda k: (-support[k], k))[0]
        groups.setdefault((std, c.hardness.value), []).append(c)
    groups = {k: v for k, v in groups.items() if len(v) >= min_members}
    if not groups:
        return list(constraints)
    member_group = {m.constraint_id: k for k, v in groups.items() for m in v}
    last = {k: v[-1].constraint_id for k, v in groups.items()}
    out: list[BuyerConstraint] = []
    for c in constraints:
        k = member_group.get(c.constraint_id)
        if k is None:
            out.append(c)
            continue
        out.append(c.model_copy(update={"framing_decision": FramingDecision.MERGE}))
        if last[k] == c.constraint_id:
            out.append(_merged(k[0], groups[k]))
    return out
