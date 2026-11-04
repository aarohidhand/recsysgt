"""Deterministic-first rules (D066): no LLM, no network, pure functions.

Three resolvers, each returning a result only when it is sure; ``None`` / empty means "ambiguous -> the LLM decides"
(never guess):

* ``classify_doc_proof``  -- doc_classify: keyword/regex table over the requirement name -> (is_doc_proof, doc_name).
* ``match_align``         -- align: buyer attribute name <-> catalogue attribute names (normalised tokens + synonyms).
* ``find_claim_quote``    -- doc_claim: required standard id next to a conformity word -> literal listing substring.

Every rule-resolved item carries the call id ``rule:<kind>`` (like ``prefilter:no_term_overlap``).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

RULE_DOC_CLASSIFY = "rule:doc_classify"
RULE_ALIGN = "rule:align"
RULE_DOC_CLAIM = "rule:doc_claim"

# ------------------------------------------------------------------ doc_classify
_STD = re.compile(r"\b(ISO|IEC|IS|BIS|EN|ASTM|DIN|BS)\b[\s/:.-]*(\d[\w.:/-]*)", re.I)
_NUMBER = re.compile(r"^\s*[-+]?\d[\d,]*(?:\.\d+)?\s*$")
_COMPARATORS = {"<", "<=", ">", ">=", "=<", "=>", "between", "range"}
# Attribute words that show the requirement is a measurable quantity/date even if it mentions a certificate.
_MEASURABLE = re.compile(
    r"\b(validity|valid for|duration|period|months?|years? of|days?|age|count|number of|quantity|cost|price|fee|"
    r"amount|turnover|date|address|name of)\b", re.I)
# Ordered table: first match wins.  (pattern, doc_name or None to derive)
_DOC_TABLE: list[tuple[re.Pattern, Optional[str]]] = [
    (re.compile(r"\bwarranty\s+(?:card|certificate)\b", re.I), "warranty card"),
    (re.compile(r"\b(?:msds|safety\s+data\s+sheet)\b", re.I), "MSDS"),
    (re.compile(r"\bcalibration\s+(?:certificate|report)\b", re.I), "calibration certificate"),
    (re.compile(r"\btype\s+test\b", re.I), "type test report"),
    (re.compile(r"\btest\s+(?:report|certificate)s?\b|\binspection\s+report\b", re.I), "test report"),
    (re.compile(r"\bgst\s+(?:certificate|registration)\b", re.I), "GST certificate"),
    (re.compile(r"\boem\s+(?:authori[sz]ation|authori[sz]ed)\b", re.I), "OEM authorisation letter"),
    (re.compile(r"\b(?:data\s*sheet|datasheet)\b", re.I), "datasheet"),
    (re.compile(r"\b(?:bis|isi)\b.*\blicen[cs]e\b|\blicen[cs]e\b.*\b(?:bis|isi)\b", re.I), "BIS licence"),
    (re.compile(r"\blicen[cs]e\b", re.I), "licence"),
    (re.compile(r"\b(?:udyam|msme|registration)\s+(?:certificate|number)\b|\bregistration\s+certificate\b", re.I),
     "registration certificate"),
    (re.compile(r"\bcertificat(?:e|es|ion|ed)\b", re.I), None),
]


def _clean(s: str) -> str:
    return " ".join(re.sub(r"[_\-/]+", " ", s or "").split())


def _std_id(text: str) -> Optional[str]:
    m = _STD.search(text or "")
    if not m:
        return None
    return f"{m.group(1).upper()} {m.group(2).rstrip('.:/-')}"


def classify_doc_proof(attribute: str, spec_name: Optional[str], operator: Optional[str],
                       value: Optional[str], unit: Optional[str] = None) -> Optional[tuple[bool, str]]:
    """``(True, doc_name)`` when the requirement NAME clearly asks for a document, else ``None`` (-> LLM).

    Conservative: a requirement carrying a numeric value with a comparison operator or a unit is measurable and is
    never rule-classified; a bare standard id with no document word ("as per IS 2062" as a material value) is left
    to the LLM; names that denote a date/period/number/address are left to the LLM.
    """
    name = _clean(f"{attribute or ''} {spec_name or ''}")
    if not name:
        return None
    v = (value or "").strip()
    if (unit and unit.strip()) or (v and _NUMBER.match(v)) or (str(operator or "").strip() in _COMPARATORS and v):
        return None
    if _MEASURABLE.search(name):
        return None
    for pat, doc in _DOC_TABLE:
        if not pat.search(name):
            continue
        if doc is not None:
            return True, doc
        sid = _std_id(name)
        if sid:
            return True, f"{sid} certificate"
        if re.search(r"\b(?:bis|isi)\b", name, re.I):
            return True, "BIS certificate"
        label = (spec_name or attribute or "").strip()
        label = _clean(label)[:60].strip()
        return (True, label) if label else None
    return None


# ------------------------------------------------------------------ align
ALIGN_EXACT_CONF = 0.95        # identical normalised name
ALIGN_SAME_TOKENS_CONF = 0.9   # same token set, different order / separators / plural
ALIGN_SYNONYM_CONF = 0.85      # curated synonym group
ALIGN_TOKEN_CONF = 0.8         # token overlap >= ALIGN_MIN_JACCARD
ALIGN_MIN_JACCARD = 0.8
ALIGN_MAX_MATCHES = 3
_STOP_TOKENS = frozenset({"of", "the", "a", "an", "and", "for", "in", "to", "type"})
_OPPOSITES = frozenset({"min", "max", "minimum", "maximum", "lower", "upper", "rated", "peak", "nominal"})

# Curated synonym groups (normalised phrases; plural-stripped, lowercase, tokens joined by a space).
_SYNONYM_GROUPS: list[frozenset[str]] = [frozenset(g) for g in (
    ("hp", "horsepower", "horse power", "motor rating", "motor power", "motor hp", "motor horsepower",
     "power rating", "power hp", "rated power hp", "motor capacity hp"),
    ("discharge head", "head", "max head", "maximum head", "total head", "pump head", "delivery head", "head m"),
    ("bore size", "borewell size", "bore well size", "borewell diameter", "bore diameter", "bore well diameter"),
    ("flow rate", "discharge", "capacity lph", "flow", "discharge rate", "pump discharge", "pump flow"),
    ("delivery size", "outlet size", "delivery pipe size", "discharge size", "outlet diameter"),
    ("suction size", "inlet size", "suction pipe size", "inlet diameter"),
    ("voltage", "rated voltage", "operating voltage", "supply voltage", "volt"),
    ("warranty", "warranty period", "guarantee", "guarantee period", "warranty year"),
    ("material", "body material", "material of construction", "construction material"),
    ("weight", "net weight", "unit weight", "product weight"),
    ("country of origin", "origin", "made in"),
    ("brand", "make", "manufacturer", "brand name", "make brand"),
)]


def _singular(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("es") and tok[-3] in "sxz":
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def name_tokens(name: str) -> tuple[str, ...]:
    """Lowercase, split on non-alphanumerics, drop stop words, singularise.  Order preserved."""
    toks = [_singular(t) for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t]
    return tuple(t for t in toks if t not in _STOP_TOKENS)


def _phrase(name: str) -> str:
    return " ".join(name_tokens(name))


_SYN_INDEX: dict[str, set[int]] = {}
for _i, _g in enumerate(_SYNONYM_GROUPS):
    for _p in _g:
        _SYN_INDEX.setdefault(_phrase(_p), set()).add(_i)


def _name_score(buyer: str, cand: str) -> float:
    """Confidence that two attribute names denote the same specification; 0.0 = no rule match."""
    bt, ct = name_tokens(buyer), name_tokens(cand)
    if not bt or not ct:
        return 0.0
    if bt == ct:
        return ALIGN_EXACT_CONF
    bs, cs = set(bt), set(ct)
    if bs == cs:
        return ALIGN_SAME_TOKENS_CONF
    if _SYN_INDEX.get(" ".join(bt), set()) & _SYN_INDEX.get(" ".join(ct), set()):
        return ALIGN_SYNONYM_CONF
    if (bs ^ cs) & _OPPOSITES:
        return 0.0
    if len(bs & cs) / len(bs | cs) >= ALIGN_MIN_JACCARD:
        return ALIGN_TOKEN_CONF
    return 0.0


def match_align(buyer_attribute: str, buyer_spec_name: Optional[str],
                candidates: Iterable[tuple[str, Optional[str]]]) -> list[tuple[str, float]]:
    """Deterministic name matches ``[(candidate_attribute, confidence)]`` (best first, <= 3), possibly empty.

    ``candidates`` are ``(attribute, spec_name_label)`` pairs.  An attribute name is compared with the buyer attribute;
    a candidate label is compared with the buyer spec name (when both exist) and with the buyer attribute.
    """
    best: dict[str, float] = {}
    for attr, label in candidates:
        s = _name_score(buyer_attribute, attr)
        if label:
            s = max(s, _name_score(buyer_attribute, label) * 0.95,
                    _name_score(buyer_spec_name, label) if buyer_spec_name else 0.0)
        if s > 0 and s > best.get(attr, 0.0):
            best[attr] = round(s, 4)
    ranked = sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:ALIGN_MAX_MATCHES]


# ------------------------------------------------------------------ doc_claim (D066, widened D071)
# A positive rule result is ALWAYS claims=True with a literal listing span; the rule never decides "no claim"
# (only the term-overlap prefilter does).  Everything ambiguous (negation, hedge, other standard in between,
# requirement-for-customer wording, different Part / star level) is left to the LLM.
CLAIM_WINDOW_CHARS = 60
CLAIM_MAX_QUOTE_CHARS = 200

# Conformity vocabulary: (regex, strong).  Strong words may anchor the doc-name-word fallback (rule (e));
# weak words ("as per", "tested", "marked", "meets", "holds") only anchor a recognised standard id.
_CONFORMITY_TABLE: list[tuple[str, bool]] = [
    (r"certified|certification|certificate|certifies", True),
    (r"conforms?|conforming|conformity|conformance|conformant", True),
    (r"compliant|compliance|complies|complying", True),
    (r"licen[cs]ed|licen[cs]e", True),
    (r"approved|approval", True),
    (r"accredited|accreditation", True),
    (r"registered|registration", True),
    (r"test\s+report|tested|testing", False),
    (r"as\s+per", False),
    (r"meets?|meeting", False),
    (r"holds?|holding", False),
    (r"marked", False),
]
_CONFORMITY = re.compile(r"\b(?:" + "|".join(w for w, _ in _CONFORMITY_TABLE) + r")\b", re.I)
_CONFORMITY_STRONG = re.compile(r"\b(?:" + "|".join(w for w, strong in _CONFORMITY_TABLE if strong) + r")\b", re.I)

_CONFORMITY_STRONG_OR_TEST = re.compile(
    r"\b(?:" + "|".join(w for w, strong in _CONFORMITY_TABLE if strong or w.startswith("test")) + r")\b", re.I)

# Negation / hedge / conditional / requirement vocabulary: any hit near the claim -> ambiguous -> LLM.
_HEDGE_WORDS: tuple[str, ...] = (
    r"not", r"no", r"non", r"nor", r"never", r"none", r"without", r"lacks?", r"lacking", r"absent", r"cannot",
    r"can", r"could", r"would", r"should", r"shall", r"may", r"might", r"must", r"will", r"won'?t", r"yet",
    r"(?:does|do|did|is|are|was|were|has|have|had)n'?t", r"does\s+not", r"do\s+not", r"is\s+not", r"are\s+not",
    r"pending", r"applied", r"applying", r"apply", r"awaiting", r"await", r"soon", r"planned?", r"planning",
    r"upcoming", r"future", r"process\s+of", r"under\s+process", r"in\s+progress", r"under\s+way", r"underway",
    r"on\s+request", r"upon\s+request", r"on\s+demand", r"if", r"unless", r"until", r"once", r"when",
    r"required?", r"requires?", r"requirements?", r"requested", r"optional", r"subject\s+to", r"depends?",
    r"can\s+be\s+arranged", r"can\s+provide", r"can\s+supply", r"able\s+to", r"available", r"on\s+order",
    r"n/a", r"na", r"expired", r"expiry", r"expires", r"lapsed", r"revoked", r"suspended", r"cancell?ed",
    r"withdrawn", r"invalid", r"former(?:ly)?", r"previously", r"was", r"were", r"expect\w*", r"aiming", r"targeting",
    r"likely", r"eligible", r"seeking", r"looking", r"ask", r"enquire", r"inquire", r"contact",
)
_HEDGE = re.compile(r"\b(?:" + "|".join(_HEDGE_WORDS) + r")\b", re.I)
_SENT_BREAK = re.compile(r"[.;!?\n|](?=\s|$)")
HEDGE_BEFORE_CHARS, HEDGE_AFTER_CHARS = 80, 80

# Numbered standard bodies: (body, case_sensitive).  Lower-case "is", "en", "bs", "ul", "ce" are English words.
_NUMBERED_BODIES: list[tuple[str, bool]] = [
    ("ISO", False), ("IEC", False), ("ASTM", False), ("DIN", False), ("IS", True), ("EN", True), ("BS", True),
    ("UL", True),
]
_ID_PREFIX = re.compile(
    r"\b(" + "|".join(b for b, _ in _NUMBERED_BODIES) + r")\b[\s/:.-]*(\d+)", re.I)
_CASE_SENSITIVE = {b for b, cs in _NUMBERED_BODIES if cs}
_ANY_ID = re.compile(
    r"\b(?:(?-i:IS|EN|BS|UL|CE)|ISO|IEC|ASTM|DIN|BIS|ISI|BEE|FSSAI|RoHS|NABL|Woolmark|GOTS|OEKO[\s-]?TEX|MSDS)\b"
    r"(?:[\s/:.-]*\d+)?", re.I)
_PART = re.compile(r"\bpart[\s.-]*(\d+)", re.I)

# Named (non-numbered) standards / marks as DATA: (name, trigger regex on doc_name, listing regex, case_sensitive).
# The trigger decides whether the requirement names the mark; the listing regex finds the mark in the listing.
_NAMED_STANDARDS: list[tuple[str, str, str, bool]] = [
    ("BIS/ISI mark", r"\b(?:BIS|ISI)\b", r"\b(?:BIS|ISI)\b", False),
    ("BEE star", r"\bBEE\b|\bstar\s+rating\b", r"\bBEE\b", False),
    ("FSSAI", r"\bFSSAI\b", r"\bFSSAI\b", False),
    ("RoHS", r"\bRoHS\b", r"\bRoHS\b", False),
    ("NABL", r"\bNABL\b", r"\bNABL\b", False),
    ("CE mark", r"\bCE\b", r"\bCE\b", True),
    ("Woolmark", r"\bWoolmark\b", r"\bWoolmark\b", False),
    ("GOTS", r"\bGOTS\b", r"\bGOTS\b", False),
    ("OEKO-TEX", r"\bOEKO[\s-]?TEX\b", r"\bOEKO[\s-]?TEX\b", False),
    ("MSDS", r"\bMSDS\b|\bsafety\s+data\s+sheet\b", r"\bMSDS\b|\bsafety\s+data\s+sheet\b", False),
    ("Type test report", r"\btype\s+test", r"\btype[\s-]*test(?:ed|s)?\b", False),
]
# Rule (e): doc-name words that carry no identity; and words so generic that a hit proves nothing.
_FALLBACK_NOISE = frozenset({
    "certificate", "certificates", "certification", "certified", "report", "reports", "document", "documents",
    "copy", "letter", "valid", "of", "the", "for", "and", "a", "an", "from", "with", "proof", "scan", "latest",
    "test", "tests", "authority", "authorised", "authorized", "approval", "licence", "license", "registration"})
_FALLBACK_GENERIC = frozenset({
    "warranty", "guarantee", "quality", "product", "products", "supplier", "company", "gst", "oem", "authorisation",
    "authorization", "brand", "manufacturer", "inspection", "pan", "tax", "invoice", "bank", "gem", "tender"})
_MIN_FALLBACK_TOKEN = 5


def _word_re(phrase_tokens: list[str]) -> re.Pattern:
    return re.compile(r"\b" + r"[\s-]*".join(re.escape(t) for t in phrase_tokens) + r"\b", re.I)


def standard_patterns(doc_name: str) -> list[re.Pattern]:
    """Regexes locating the standard / mark named in ``doc_name`` inside a listing.

    Numbered ids (``ISO 9001:2015``, ``IS 302 (Part 1):2014``, ``IS: 14846``, ``EN 71``) -> ``BODY[sep]NUM`` not
    followed by a digit; else the named-standards table (BIS/ISI, BEE star, FSSAI, RoHS, NABL, CE, Woolmark, GOTS,
    OEKO-TEX, MSDS, type test report); else (rule (e)) the literal distinctive words of the doc name."""
    name = doc_name or ""
    pats: list[re.Pattern] = []
    for m in _ID_PREFIX.finditer(name):
        pre, num = m.group(1), m.group(2)
        flag = f"(?-i:{pre.upper()})" if pre.upper() in _CASE_SENSITIVE else re.escape(pre)
        pats.append(re.compile(rf"\b{flag}[\s/:.-]*{re.escape(num)}(?!\d)", re.I))
    if pats:
        return pats
    for _n, trig, lst, cs in _NAMED_STANDARDS:
        if re.search(trig, name, 0 if cs else re.I):
            star = re.search(r"(\d)\s*-?\s*star", name, re.I) if _n == "BEE star" else None
            if star:
                n = star.group(1)
                pats.append(re.compile(
                    rf"\bBEE\b[^.;\n]{{0,25}}?\b{n}\s*-?\s*star|\b{n}\s*-?\s*star[^.;\n]{{0,25}}?\bBEE\b", re.I))
            else:
                pats.append(re.compile(lst, 0 if cs else re.I))
    if pats:
        return pats
    return _fallback_patterns(name)


def _fallback_patterns(doc_name: str) -> list[re.Pattern]:
    toks = [t for t in re.split(r"[^a-z0-9]+", doc_name.lower()) if t]
    core = [t for t in toks if t not in _FALLBACK_NOISE]
    if not core or any(t in _FALLBACK_GENERIC for t in core) or max(len(t) for t in core) < _MIN_FALLBACK_TOKEN:
        return []
    return [_word_re(core)]


def _is_fallback(doc_name: str) -> bool:
    return not _ID_PREFIX.search(doc_name or "") and not any(
        re.search(trig, doc_name or "", 0 if cs else re.I) for _n, trig, _l, cs in _NAMED_STANDARDS)


def find_claim_quote(listing: str, doc_name: str) -> Optional[str]:
    """A literal listing substring (<= 200 chars) in which the required standard id sits within ~60 chars of a
    conformity word, or ``None`` when absent / hedged / negated / unclear (-> LLM).  The caller must still run
    ``verify_quote`` (done by the runner) before treating it as a claim."""
    if not listing or not doc_name:
        return None
    fallback = _is_fallback(doc_name)
    # rule (e): only strong words anchor a doc-name-word match, plus test words when the document itself is a test report
    conformity = _CONFORMITY
    if fallback:
        conformity = _CONFORMITY_STRONG_OR_TEST if re.search(r"\btest", doc_name, re.I) else _CONFORMITY_STRONG
    want_part = _PART.search(doc_name)
    for pat in standard_patterns(doc_name):
        for m in pat.finditer(listing):
            if want_part:                     # IS 302 (Part 1) must not be satisfied by Part 2
                got = _PART.search(listing[m.end():m.end() + 20])
                if got and got.group(1) != want_part.group(1):
                    continue
            lo, hi = max(0, m.start() - CLAIM_WINDOW_CHARS), min(len(listing), m.end() + CLAIM_WINDOW_CHARS)
            window = listing[lo:hi]
            best: Optional[tuple[int, int, int]] = None
            for w in conformity.finditer(window):
                ws, we = lo + w.start(), lo + w.end()
                dist = max(ws - m.end(), m.start() - we, 0)
                if dist > CLAIM_WINDOW_CHARS:
                    continue
                gap = listing[m.end():ws] if ws >= m.end() else listing[we:m.start()]
                if _ANY_ID.search(gap):       # another standard sits between: the conformity word may belong to it
                    continue
                if best is None or dist < best[0]:
                    best = (dist, ws, we)
            if best is None:
                continue
            _, ws, we = best
            start, end = min(m.start(), ws), max(m.end(), we)
            if end - start > CLAIM_MAX_QUOTE_CHARS:
                continue
            if _hedged(listing, start, end):
                continue
            return listing[start:end].strip()
    return None


def _hedged(listing: str, start: int, end: int) -> bool:
    """Negation / hedge / requirement wording in the same sentence, within a char window around the span."""
    left = 0
    for b in _SENT_BREAK.finditer(listing, 0, start):
        left = b.end()
    nxt = _SENT_BREAK.search(listing, end)
    right = nxt.start() if nxt else len(listing)
    if "?" in listing[end:end + 2]:            # a question is not an assertion
        return True
    lo, hi = max(left, start - HEDGE_BEFORE_CHARS), min(right, end + HEDGE_AFTER_CHARS)
    return bool(_HEDGE.search(listing[lo:hi]))


# ------------------------------------------------------------------ doc_claim post-check for LLM claims (D073)
# verify_quote only proves that the quote exists in the listing.  The check below proves that the quote is about the
# REQUIRED document: it must contain the standard id / named mark of the doc name, or at least one distinctive word of it.
# Generic words (certificate, report, test, ...) and "ISO certified" alone never prove a different document.
_QUOTE_STOP = frozenset({"of", "the", "for", "and", "a", "an", "from", "with", "to", "in", "on", "by", "or", "as", "per",
                         "copy", "letter", "valid", "latest", "scan", "proof", "document", "documents"})
_QUOTE_NOISE = _FALLBACK_NOISE | _QUOTE_STOP


def _word_hit(tok: str, text: str) -> bool:
    stem = tok if len(tok) <= 6 else tok[:max(6, len(tok) - 2)]
    return re.search(r"\b" + re.escape(stem), text, re.I) is not None


def claim_quote_supports(quote: str, doc_name: str) -> bool:
    """True when ``quote`` can be about ``doc_name``: standard id / named mark of the doc name occurs in the quote, else
    (no id, no mark) at least one distinctive word of the doc name occurs.  A doc name made only of generic words
    ("test report") needs a test word in the quote; a bare "IS" needs a stand-alone IS / BIS / ISI.  Doc names with
    nothing checkable are accepted (the check never invents a rejection)."""
    quote, name = quote or "", doc_name or ""
    if not quote or not name:
        return True
    if _ID_PREFIX.search(name) or not _is_fallback(name):
        return any(p.search(quote) for p in standard_patterns(name))
    toks = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]
    distinct = [t for t in toks if t not in _QUOTE_NOISE and t != "is" and len(t) >= 3]
    if distinct:
        return any(_word_hit(t, quote) for t in distinct)
    if re.search(r"(?-i:\bIS\b)", name):
        return re.search(r"(?-i:\bIS\b)|\bBIS\b|\bISI\b", quote) is not None
    if any(t in ("test", "tests") for t in toks):
        return re.search(r"\btest(?:s|ed|ing)?\b", quote, re.I) is not None
    return True
