"""D071: claim cache + dedup for doc_claim (D051).

OWNER: data-steward. Pure, no network, no LLM.

A document claim depends only on (listing text actually sent, required standard/document). The key therefore is

    claim_key = sha256(normalised_doc_name, sha256(listing_text))

* ``listing_text`` is hashed EXACTLY as sent (the caller passes the text after truncation to
  ``doc_claims.max_listing_chars``); a changed listing (one character) is a different key.
* The doc name is normalised conservatively: casefold, standard ids canonicalised (``IS 302`` / ``is-302`` / ``IS:302`` ->
  ``is 302``; ``ISO 9001:2015`` -> ``iso 9001:2015``), remaining punctuation and whitespace collapsed. Equality is exact on
  the normalised string: ``IS 302`` != ``IS 3021``, ``ISO 9001`` != ``ISO 9001:2015``. No fuzzy matching.

Identical key  <=>  identical (listing, standard); the stored result is then valid for every pair sharing it. Per-pair
fields (``product_id``, ``constraint_ref``) are set at broadcast time, and the quote is re-verified against the pair's own
listing text.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

from gtgen.contracts import DocClaim

logger = logging.getLogger(__name__)

PATHS = ("prefilter", "rule", "llm")
_STD = re.compile(r"\b(is|iso|iec|bis|en|astm|din|bs|ansi|ieee|ul|nabl|nema)\s*[-:/.\s]?\s*(\d[\d]*(?:\s*[-:/.]\s*\d+)*)",
                  re.IGNORECASE)
_PUNCT = re.compile(r"[^\w:]+", re.UNICODE)


def _std_sub(m: "re.Match[str]") -> str:
    num = re.sub(r"\s*([-:/.])\s*", r"\1", m.group(2))
    return f" {m.group(1).lower()} {num} "


def normalise_doc_name(doc_name: str) -> str:
    """Casefold, canonicalise standard ids, collapse punctuation/whitespace. Exact-equality key text (never fuzzy)."""
    s = (doc_name or "").casefold()
    s = _STD.sub(_std_sub, s)
    s = s.replace(" ", " ")
    s = _PUNCT.sub(" ", s)
    return " ".join(s.split())


def listing_hash(listing_text: str) -> str:
    return hashlib.sha256((listing_text or "").encode("utf-8")).hexdigest()


def claim_key(listing_text: str, doc_name: str) -> str:
    h = hashlib.sha256()
    h.update(normalise_doc_name(doc_name).encode("utf-8"))
    h.update(b"\x00")
    h.update(listing_hash(listing_text).encode("ascii"))
    return h.hexdigest()


def _verified(quote: Optional[str], listing_text: str) -> bool:
    from gtgen.llm.claims import verify_quote
    return verify_quote(quote, listing_text)


def path_of(llm_call_id: str) -> str:
    """prefilter | rule | llm from the DocClaim.llm_call_id the runners/data layer write."""
    if llm_call_id.startswith("prefilter:"):
        return "prefilter"
    if llm_call_id.startswith("rule:"):
        return "rule"
    return "llm"


class ClaimCache:
    """key -> {claims, quote, quote_verified, path, llm_call_id}. Failed results are never stored (no information)."""

    def __init__(self, entries: Optional[Mapping[str, Mapping[str, Any]]] = None):
        self._e: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (entries or {}).items()}
        self.hits = 0
        self.misses = 0
        self.dedup = 0          # pairs answered from an entry created for another pair in the same fetch

    def __len__(self) -> int:
        return len(self._e)

    def __contains__(self, key: str) -> bool:
        return key in self._e

    # ---------------------------------------------------------------- get / put
    def get(self, key: str) -> Optional[dict]:
        e = self._e.get(key)
        if e is None:
            self.misses += 1
            return None
        self.hits += 1
        return dict(e)

    def put(self, key: str, *, claims: bool, quote: Optional[str], quote_verified: bool, path: str,
            llm_call_id: str) -> None:
        if path not in PATHS:
            raise ValueError(f"path must be one of {PATHS}, got {path!r}")
        if claims and not (quote and quote_verified):
            raise ValueError("a claim needs a verified quote")
        self._e[key] = {"claims": bool(claims), "quote": quote, "quote_verified": bool(quote_verified),
                        "path": path, "llm_call_id": llm_call_id}

    def put_claim(self, request: Any, claim: DocClaim) -> bool:
        """Store the resolved DocClaim of ``request`` (DocClaimRequest-like). Failed claims are not stored; returns stored?"""
        if claim.llm_call_id == "failed":
            return False
        self.put(claim_key(request.listing_text, request.doc_name), claims=claim.claims, quote=claim.quote,
                 quote_verified=claim.quote_verified, path=path_of(claim.llm_call_id), llm_call_id=claim.llm_call_id)
        return True

    def put_claims(self, requests: Sequence[Any], claims: Sequence[DocClaim]) -> int:
        """Store LLM results for ``requests`` (aligned). Returns the number stored."""
        return sum(self.put_claim(r, c) for r, c in zip(requests, claims))

    # ---------------------------------------------------------------- broadcast
    def _build(self, r: Any, e: Mapping[str, Any]) -> DocClaim:
        quote = e.get("quote")
        ok = bool(e["claims"]) and _verified(quote, r.listing_text)   # verify against THIS pair's listing
        return DocClaim(product_id=r.product_id, constraint_ref=r.constraint_ref, claims=ok, quote=quote,
                        quote_verified=ok, llm_call_id=e["llm_call_id"])

    def broadcast(self, requests: Iterable[Any]) -> list[Optional[DocClaim]]:
        """One DocClaim per request (aligned; ``None`` where the key is not cached), ``constraint_ref``/``product_id``
        taken from each request. Does not touch the hit/miss counters."""
        out: list[Optional[DocClaim]] = []
        for r in requests:
            e = self._e.get(claim_key(r.listing_text, r.doc_name))
            out.append(self._build(r, e) if e is not None else None)
        return out

    def lookup_for_dialogue(self, product_id: str, listing_text: str, doc_name: str) -> Optional[dict]:
        """Cached entry ``{claims, quote, quote_verified, path, llm_call_id}`` for this (listing, standard) or None.
        ``product_id`` is accepted for call-site symmetry and logging only; it is NOT part of the key (two products with
        the identical listing text share the answer). Counts a hit/miss. The quote is re-verified against
        ``listing_text`` (a stale entry cannot assert a claim the listing does not contain)."""
        e = self.get(claim_key(listing_text, doc_name))
        if e is None:
            return None
        if e["claims"] and not _verified(e.get("quote"), listing_text):
            e["claims"], e["quote_verified"] = False, False
        return e

    # ---------------------------------------------------------------- (de)serialisation
    def to_json(self) -> dict:
        return {"version": 1, "entries": {k: dict(v) for k, v in sorted(self._e.items())},
                "counters": {"hits": self.hits, "misses": self.misses, "dedup": self.dedup}}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "ClaimCache":
        c = cls(d.get("entries", {}))
        k = d.get("counters", {})
        c.hits, c.misses, c.dedup = int(k.get("hits", 0)), int(k.get("misses", 0)), int(k.get("dedup", 0))
        return c


def build_dialogue_claims(cache: Optional[ClaimCache], requests: Sequence[Any]
                          ) -> tuple[dict[tuple[str, str], DocClaim], list]:
    """Split dialogue ``requests`` (DocClaimRequest-like; ``constraint_ref`` may be a BELIEF id) into

    * ``known``: ``{(product_id, constraint_ref): DocClaim}`` for every request whose (listing, standard) is cached;
    * ``missing``: ONE representative request per unique missing key (ask the LLM only for these), in input order.

    After the LLM answers ``missing``: ``cache.put_claims(missing, claims)`` then
    ``build_dialogue_claims(cache, requests)`` again (or ``cache.broadcast``) fills the remaining pairs that share a key.
    ``cache=None`` -> everything is missing (deduplicated). Counts hits/misses/dedup on the cache.
    """
    known: dict[tuple[str, str], DocClaim] = {}
    missing: list = []
    seen: set[str] = set()
    for r in requests:
        k = claim_key(r.listing_text, r.doc_name)
        e = cache._e.get(k) if cache is not None else None
        if e is not None:
            known[(r.product_id, r.constraint_ref)] = cache._build(r, e)
            cache.hits += 1
        elif k in seen:
            if cache is not None:
                cache.dedup += 1
        else:
            seen.add(k)
            missing.append(r)
            if cache is not None:
                cache.misses += 1
    return known, missing
