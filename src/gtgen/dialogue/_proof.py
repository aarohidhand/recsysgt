"""D055 proof requests: the document-name convention (buyer side) and the transcript-only detector (negotiator side).

Buyer: ``proof_doc_name(constraint)`` is the document the buyer asks for: the constraint's ``doc_name`` for a doc-proof
constraint, else "<attribute words> test report". The buyer's utterance must name it exactly (the runner checks this).

Negotiator: ``detect_doc_phrases(utterance)`` finds "<words> certificate/test report/..." phrases in the buyer's LAST
message, but only when a proof-intent word is present. The GT negotiator then links a phrase to one of its own beliefs by
token overlap and to a catalogue claim (a PASS / PENDING cell) before it acknowledges. Nothing here reads buyer state.
"""
from __future__ import annotations

import re
from typing import Optional

from gtgen.contracts import BuyerConstraint

DOC_NOUN_RE = r"(?:test\s+report|certificate|certification|datasheet|data\s+sheet|declaration|report|documentation|document|licen[cs]e)"
_PHRASE = re.compile(rf"\b((?:[\w:/.\-]+\s+){{1,5}}{DOC_NOUN_RE})\b", re.I)
_INTENT = re.compile(r"\b(proof|prove|send|share|copy|copies|provide|verify|verification|evidence|attach|submit|show|"
                     r"documentation)\b", re.I)
_LEAD = frozenset({
    "please", "kindly", "send", "share", "provide", "submit", "attach", "show", "us", "me", "the", "a", "an", "your",
    "their", "its", "copy", "copies", "of", "for", "to", "and", "also", "we", "would", "like", "need", "want", "can",
    "could", "you", "that", "this", "with", "valid", "current", "latest", "proof", "prove", "verify", "ask", "asking",
    "before", "is", "are", "be", "have", "get", "see", "i", "our", "any", "some", "supplier", "supplier's",
})
_TRAIL_NOUN = re.compile(rf"\s*{DOC_NOUN_RE}\s*$", re.I)


def humanise(attribute: str) -> str:
    return re.sub(r"\s+", " ", (attribute or "").replace("_", " ")).strip()


def proof_doc_name(c: BuyerConstraint) -> str:
    """The document name a buyer uses when it asks for proof of constraint ``c``."""
    if c.is_doc_proof:
        return (c.doc_name or humanise(c.attribute)).strip()
    return f"{humanise(c.attribute)} test report"


def detect_doc_phrases(utterance: str) -> list[str]:
    """Document phrases in a buyer message (exactly as written), only if a proof-intent word occurs. [] otherwise."""
    if not _INTENT.search(utterance or ""):
        return []
    out: list[str] = []
    for m in _PHRASE.finditer(utterance or ""):
        words = m.group(1).split()
        while words and words[0].lower().strip(",.;:") in _LEAD:
            words.pop(0)
        if len(words) >= 2:                      # at least one content word plus the document noun
            phrase = " ".join(words).strip(" ,.;:")
            if phrase and phrase not in out:
                out.append(phrase)
    return out


def phrase_subject(phrase: str) -> str:
    """The phrase without its trailing document noun ("ISO 9001 certificate" -> "ISO 9001")."""
    return _TRAIL_NOUN.sub("", phrase).strip()


def first_phrase_for(utterance: str, subject_tokens: set[str], tokenise) -> Optional[str]:
    """First document phrase of ``utterance`` whose subject shares >= half of its tokens with ``subject_tokens``."""
    for ph in detect_doc_phrases(utterance):
        toks = tokenise(phrase_subject(ph))
        if toks and len(toks & subject_tokens) / len(toks) >= 0.5:
            return ph
    return None
