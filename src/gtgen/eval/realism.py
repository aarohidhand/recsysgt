"""D061 deterministic realism checks (the 5-paragraph judgment part belongs to the gt-auditor).

Per conversation (opening = first BUYER turn):
  opening_sentences   count of sentences (split after . ! ? or newline) within [opening_min, opening_max]_sentences
  opening_greeting    one of hi/hello/hey/dear/greetings/namaste/good morning|afternoon|evening within the first 6 words
  opening_no_list     no line starting with a bullet (-, *, bullet char) or a numbered marker (1. / 2) )
  opening_reveal      |revealed_constraint_ids of that turn ∩ brief ids| / |brief ids| <= opening_max_constraints_revealed_share
                      (brief ids = all brief constraints whose scenario_role is not 'dropped'; applies even for tiny briefs)
  budget              needs the CatalogueView: unit prices > 0; cheapest = min, med = median;
                      OK iff  f_lo * quantity * cheapest <= budget_inr <= f_hi * quantity * med   (f = budget_catalogue_factor)
                      MOQ fit = share of candidates with moq None or moq <= quantity (WARN when no candidate fits)
  no_inline_tags      (D065) no bracket tag [HARD] / [SOFT ..] / [NEW ..] / [PASS] / [FAIL] in any utterance (forbid_inline_tags)
  no_spoken_arithmetic (D065) NEGOTIATOR turns: no "joint gain", "cost 0.x", "N candidates .. M feasible", "feasible count",
                      "p_feasible", "score 0.x", "gain of N" (forbid_spoken_arithmetic)
  no_supplier_stats   (D070) neither speaker states a supplier rating or years of experience: "rated at least 4.5", "out of 5",
                      "N stars", "years of trading/experience/business", "trading history", "tenure", "in business for N years",
                      "N years in business/operation".  Product warranty/spec years ("2 years warranty") and delivery do not match
                      (forbid_supplier_stats)
  enumerated_burst    (D065) BUYER turn with more than max_enumerated_items numbered/bulleted/(a) items, on lines or inline
                      Each D065 check reports the offending turn indexes in Check.data["turns"].
Run level:
  sales_persona       distinct names (scenario_meta["sales_persona"]["name"], or the string itself) across scenarios;
                      FAIL if >1 scenarios and one distinct persona, WARN if some repeat, SKIP if none recorded
  turn_repeat         pairs of utterances (any speaker, whole run) with text_similarity >= turn_repeat_similarity;
                      utterances under 6 words are excluded (fixed greetings); FAIL if any pair
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Mapping, Optional, Sequence

from gtgen.contracts import Action, CatalogueView, GTConversation, Speaker
from gtgen.matching.compliance import RANKING_ONLY_ATTRIBUTES
from gtgen.eval._report import FAIL, PASS, SKIP, WARN, Check, norm_text, text_similarity, worst

_BULLET = re.compile(r"^\s*(?:[-*•●–]|\d+\s*[.)])\s+")
_GREET = re.compile(r"^\W*(?:hi|hello|hey|hiya|dear|greetings|namaste|good\s+(?:morning|afternoon|evening|day))\b", re.I)
_MIN_WORDS = 6
_TAG = re.compile(r"\[\s*(?:hard|soft|new|pass|fail)\b[^\]\n]*\]", re.I)
_ARITH = [re.compile(p, re.I) for p in (
    r"\bjoint\s+gain\b", r"\bcost\s*[:=]?\s*0?\.\d+", r"\bfeasible\s+count\b", r"\bp_feasible\b",
    r"\bscore\s*[:=]?\s*0?\.\d+", r"\bgain\s+of\s+\d+\b",
    r"\b\d+\s+candidates?\b[^.\n]{0,60}?\b\d+\s+feasible\b")]
_YRS = r"\d+(?:\.\d+)?\+?\s*(?:years?|yrs?)"
_STATS = [re.compile(p, re.I) for p in (
    r"\brated\s+(?:at\s+least|above|over|under|below|around|about)\b", r"\brated\s+\d(?:\.\d+)?\s*(?:/\s*5|out\s+of|stars?|\+)",
    r"\bout\s+of\s+(?:5|five)\b", r"\b\d(?:\.\d+)?\s*\+?\s*stars?\b", r"\bstar\s+ratings?\b",
    r"\b(?:supplier|seller|vendor|customer|buyer|minimum|average)\s+ratings?\b",
    r"\bratings?\s+(?:of|at|above|over)\s+\d(?:\.\d+)?\b",
    r"\byears?\s+of\s+(?:trading|experience|business|operation|operations|history|standing|service\s+as)\b",
    r"\btrading\s+(?:history|record|years?)\b", r"\btenure\b", r"\b(?:track\s+record|experience)\s+of\s+" + _YRS,
    r"\bin\s+(?:the\s+)?business\s+(?:for|since)\b", rf"\b{_YRS}\s+(?:in\s+(?:the\s+)?(?:business|operation|trade|market)|of\s+(?:trading|experience|business))\b",
    r"\b(?:established|operating|trading|operated)\s+(?:for|since)\s+(?:over\s+|more\s+than\s+)?\d",
    rf"\b(?:at\s+least|over|more\s+than|minimum(?:\s+of)?)\s+{_YRS}\s+(?:of\s+)?(?:\w+\s+)?(?:experience|trading|business)\b")]
_NUM_MARK = re.compile(r"(?:(?<=\s)|^)(\d{1,2})[.)]\s+(?=\S)")
_LET_MARK = re.compile(r"(?:(?<=\s)|^)\(([a-z])\)\s+(?=\S)", re.I)


@dataclass
class ConversationRealism:
    scenario_id: str
    checks: list[Check]

    @property
    def verdict(self) -> str:
        return worst(self.checks)

    @property
    def flags(self) -> list[str]:
        return [c.name for c in self.checks if c.verdict in (WARN, FAIL)]


@dataclass
class RealismReport:
    conversations: list[ConversationRealism]
    run_checks: list[Check]
    verdict: str
    repeat_pairs: list[tuple[str, int, str, int, float]] = field(default_factory=list)

    def summary(self) -> dict:
        flagged = {c.scenario_id: c.flags for c in self.conversations if c.flags}
        return {"verdict": self.verdict, "flagged_conversations": flagged,
                "run_checks": {c.name: c.verdict for c in self.run_checks}}


def _cfg(settings: Optional[Mapping]) -> dict:
    cfg = {"opening_max_sentences": 5, "opening_min_sentences": 2, "opening_max_constraints_revealed_share": 0.75,
           "turn_repeat_similarity": 0.9, "budget_catalogue_factor": [0.5, 3.0],
           "max_enumerated_items": 2, "forbid_inline_tags": True, "forbid_spoken_arithmetic": True,
           "forbid_supplier_stats": True, "check_final_turn_closes": True, "check_no_deferral": True,
           "check_negotiation_depth": True}
    cfg.update(((settings or {}).get("persona", {}) or {}).get("realism", {}) or {})
    return cfg


def count_sentences(text: str) -> int:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return sum(1 for p in parts if re.search(r"\w", p))


def has_greeting(text: str) -> bool:
    return bool(_GREET.match(" ".join(text.split()[:6])))


def has_list_lines(text: str) -> bool:
    return any(_BULLET.match(line) for line in text.splitlines())


def has_inline_tag(text: str) -> bool:
    return bool(_TAG.search(text))


def has_spoken_arithmetic(text: str) -> bool:
    return any(p.search(text) for p in _ARITH)


def has_supplier_stats(text: str) -> bool:
    return any(p.search(text) for p in _STATS)


def enumerated_items(text: str) -> int:
    """Largest of: bullet/numbered line count, consecutive inline 1. 2. 3. run, distinct inline (a) (b) markers."""
    lines = sum(1 for line in text.splitlines() if _BULLET.match(line))
    nums = {int(m.group(1)) for m in _NUM_MARK.finditer(text)}
    run = 0
    while run + 1 in nums:
        run += 1
    letters = len({m.group(1).lower() for m in _LET_MARK.finditer(text)})
    return max(lines, run, letters)


_COMMIT = [re.compile(p, re.I) for p in (
    r"\bI\s*(?:will|shall|'ll|’ll)\b", r"\bwe\s*(?:will|shall|'ll|’ll)\b", r"\blet\s+me\b", r"\blet'?s\b",
    r"\bwould\s+you\s+like\b", r"\blet\s+me\s+know\b", r"\bfeel\s+free\b", r"\bshall\s+I\b",
    r"\bI(?:'m|’m|\s+am)\s+(?:going\s+to|about\s+to)\b")]
_DEFER = [re.compile(p, re.I) for p in (
    r"\blater\b", r"\bfor\s+now\b", r"\b(?:will|shall|'ll|’ll)\s+(?:discuss|share|talk|get\s+back|tell|cover)\b",
    r"\bat\s+a\s+later\b", r"\bnot\s+(?:right\s+)?now\b")]


def closing_problem(text: str) -> Optional[str]:
    if "?" in text:
        return "question"
    for r in _COMMIT:
        m = r.search(text)
        if m:
            return m.group(0).lower()
    return None


def has_deferral(text: str) -> bool:
    return any(r.search(text) for r in _DEFER)


def dialogue_checks(conv: GTConversation, cfg: Mapping) -> list[Check]:
    out: list[Check] = []
    neg = [t for t in conv.turns if t.speaker == Speaker.NEGOTIATOR]
    if cfg["check_final_turn_closes"] and neg:
        last = neg[-1]
        prob = closing_problem(last.utterance)
        out.append(Check("final_turn_closes", FAIL if prob else PASS,
                         f"negotiator final turn {last.turn_idx} does not close ({prob!r})" if prob
                         else "negotiator final turn closes", {"turns": [last.turn_idx] if prob else [], "phrase": prob}))
    if cfg["check_no_deferral"]:
        bad: list[int] = []
        clarified = False
        for t in conv.turns:
            if t.speaker == Speaker.NEGOTIATOR:
                clarified = t.action == Action.CLARIFY
            elif clarified:
                if has_deferral(t.utterance):
                    bad.append(t.turn_idx)
                clarified = False
        out.append(Check("no_deferral", FAIL if bad else PASS,
                         f"buyer defers after CLARIFY in turn(s) {bad}" if bad else "no deferral after CLARIFY",
                         {"turns": bad, "count": len(bad)}))
    if cfg["check_negotiation_depth"]:
        n_prop = sum(1 for t in conv.turns if t.action == Action.PROPOSE_RELAXATION)
        none = n_prop == 0 and conv.final.outcome == "BEST_AVAILABLE"
        out.append(Check("negotiation_depth", WARN if none else PASS,
                         "no_negotiation" if none else f"{n_prop} PROPOSE_RELAXATION turn(s)",
                         {"proposals": n_prop, "no_negotiation": none}))
    return out


def style_checks(conv: GTConversation, cfg: Mapping) -> list[Check]:
    out: list[Check] = []
    if cfg["forbid_inline_tags"]:
        bad = [t.turn_idx for t in conv.turns if has_inline_tag(t.utterance)]
        out.append(Check("no_inline_tags", FAIL if bad else PASS,
                         f"inline tag in turn(s) {bad}" if bad else "no inline tags", {"turns": bad, "count": len(bad)}))
    if cfg["forbid_spoken_arithmetic"]:
        bad = [t.turn_idx for t in conv.turns if t.speaker == Speaker.NEGOTIATOR and has_spoken_arithmetic(t.utterance)]
        out.append(Check("no_spoken_arithmetic", FAIL if bad else PASS,
                         f"spoken arithmetic in turn(s) {bad}" if bad else "no spoken arithmetic",
                         {"turns": bad, "count": len(bad)}))
    if cfg["forbid_supplier_stats"]:
        bad = [t.turn_idx for t in conv.turns if has_supplier_stats(t.utterance)]
        out.append(Check("no_supplier_stats", FAIL if bad else PASS,
                         f"supplier rating / years stated in turn(s) {bad}" if bad else "no supplier rating / years stated",
                         {"turns": bad, "count": len(bad)}))
    mx = cfg["max_enumerated_items"]
    bad = [t.turn_idx for t in conv.turns if t.speaker == Speaker.BUYER and enumerated_items(t.utterance) > mx]
    out.append(Check("enumerated_burst", FAIL if bad else PASS,
                     f"> {mx} enumerated items in buyer turn(s) {bad}" if bad else f"no buyer turn with > {mx} items",
                     {"turns": bad, "count": len(bad)}))
    return out


def opening_checks(conv: GTConversation, cfg: Mapping) -> list[Check]:
    first = next((t for t in conv.turns if t.speaker == Speaker.BUYER), None)
    if first is None:
        return [Check("opening", FAIL, "no BUYER turn")]
    u = first.utterance
    n, lo, hi = count_sentences(u), cfg["opening_min_sentences"], cfg["opening_max_sentences"]
    out = [Check("opening_sentences", PASS if lo <= n <= hi else FAIL, f"{n} sentences (allowed {lo}..{hi})", {"n": n}),
           Check("opening_greeting", PASS if has_greeting(u) else FAIL, "greeting present" if has_greeting(u) else "no greeting"),
           Check("opening_no_list", FAIL if has_list_lines(u) else PASS,
                 "bullet/numbered lines" if has_list_lines(u) else "no list formatting")]
    ids = {c.constraint_id for c in conv.commercial_brief.constraints
           if getattr(c.scenario_role, "value", c.scenario_role) != "dropped"
           and c.attribute not in RANKING_ONLY_ATTRIBUTES}               # D070: never part of the brief
    if ids:
        share = len(set(first.revealed_constraint_ids) & ids) / len(ids)
        mx = cfg["opening_max_constraints_revealed_share"]
        out.append(Check("opening_reveal", PASS if share <= mx + 1e-9 else FAIL,
                         f"{share:.2f} of constraints revealed (max {mx})", {"share": share}))
    else:
        out.append(Check("opening_reveal", SKIP, "no brief constraints"))
    return out


def budget_check(conv: GTConversation, catalogue: Optional[CatalogueView], cfg: Mapping) -> list[Check]:
    brief = conv.commercial_brief
    if catalogue is None or brief.budget_inr is None:
        return [Check("budget", SKIP, "no catalogue or no budget")]
    prices = sorted(c.price for c in catalogue.candidates if c.price is not None and c.price > 0)
    if not prices:
        return [Check("budget", SKIP, "no candidate prices")]
    q, b = brief.quantity, brief.budget_inr
    cheapest, med = prices[0], statistics.median(prices)
    f_lo, f_hi = cfg["budget_catalogue_factor"]
    lo, hi = f_lo * q * cheapest, f_hi * q * med
    ok = lo - 1e-9 <= b <= hi + 1e-9
    out = [Check("budget", PASS if ok else FAIL,
                 f"budget {b:g} vs [{lo:g}, {hi:g}] (qty {q:g}, cheapest {cheapest:g}, median {med:g})",
                 {"lo": lo, "hi": hi, "budget": b})]
    cands = catalogue.candidates
    fit = sum(1 for c in cands if c.moq is None or c.moq <= q)
    out.append(Check("moq_fit", PASS if fit else WARN, f"{fit}/{len(cands)} candidates with MOQ <= quantity",
                     {"fit": fit, "n": len(cands)}))
    return out


def sales_persona_name(conv: GTConversation) -> Optional[str]:
    sp = conv.commercial_brief.scenario_meta.get("sales_persona")
    if isinstance(sp, Mapping):
        sp = sp.get("name")
    return norm_text(str(sp)) if sp else None


def analyse(convs: Sequence[GTConversation], catalogues: Optional[Mapping[str, CatalogueView] | CatalogueView] = None,
            settings: Optional[Mapping] = None) -> RealismReport:
    cfg = _cfg(settings)
    def cat_for(sid: str) -> Optional[CatalogueView]:
        if isinstance(catalogues, CatalogueView):
            return catalogues
        return (catalogues or {}).get(sid)
    per = [ConversationRealism(c.scenario_id, opening_checks(c, cfg) + budget_check(c, cat_for(c.scenario_id), cfg)
                                      + style_checks(c, cfg) + dialogue_checks(c, cfg))
           for c in convs]

    run: list[Check] = []
    names = [(c.scenario_id, sales_persona_name(c)) for c in convs]
    got = [n for _, n in names if n]
    if not got:
        run.append(Check("sales_persona", SKIP, "no sales_persona recorded"))
    else:
        distinct = len(set(got))
        if len(convs) > 1 and distinct == 1:
            v = FAIL
        else:
            v = PASS if distinct == len(convs) else WARN
        run.append(Check("sales_persona", v, f"{distinct} distinct of {len(convs)} scenarios", {"distinct": distinct}))

    utts = [(c.scenario_id, t.turn_idx, t.utterance) for c in convs for t in c.turns
            if len(t.utterance.split()) >= _MIN_WORDS]
    thr = cfg["turn_repeat_similarity"]
    pairs = []
    for (sa, ia, ua), (sb, ib, ub) in combinations(utts, 2):
        if text_similarity(ua, ub) >= thr:
            pairs.append((sa, ia, sb, ib, text_similarity(ua, ub)))
    run.append(Check("turn_repeat", FAIL if pairs else PASS,
                     f"{len(pairs)} near-verbatim pair(s) at >= {thr}" if pairs else "no near-verbatim repeats",
                     {"pairs": pairs}))
    verdict = worst([c for p in per for c in p.checks] + run)
    return RealismReport(per, run, verdict, pairs)


def render(rep: RealismReport) -> str:
    lines = [f"Realism (D061, deterministic): verdict={rep.verdict}"]
    for p in rep.conversations:
        lines.append(f"  {p.scenario_id}: {p.verdict}" + (f"  flags: {', '.join(p.flags)}" if p.flags else ""))
        for c in p.checks:
            if c.verdict in (WARN, FAIL):
                lines.append(f"      {c.verdict} {c.name}: {c.detail}")
    for c in rep.run_checks:
        lines.append(f"  run {c.verdict:<4} {c.name}: {c.detail}")
    for sa, ia, sb, ib, s in rep.repeat_pairs:
        lines.append(f"      repeat: {sa}#{ia} ~ {sb}#{ib} (sim={s:.2f})")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gtgen.eval.realism")
    ap.add_argument("--run")
    ap.add_argument("--jsonl")
    ap.add_argument("--settings", default="config/settings.yaml")
    a = ap.parse_args(argv)
    if not (a.run or a.jsonl):
        ap.error("give --run or --jsonl")
    import yaml
    with open(a.settings, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    from gtgen.eval.conversations import load_conversations
    path = Path(a.jsonl) if a.jsonl else Path(settings["paths"]["out"]) / a.run / "conversations.jsonl"
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 1
    rep = analyse(load_conversations(path), None, settings)     # no catalogue on disk: budget check is SKIP
    print(render(rep))
    return 1 if rep.verdict == FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
