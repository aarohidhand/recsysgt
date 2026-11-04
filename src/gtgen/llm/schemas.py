"""Pydantic output schemas for every prompt (what the LLM returns), plus input helper models.

Kept flat and simple (no dict-valued fields) so that provider-side strict JSON-schema mode accepts them.
Wrappers in runners.py convert these into the contracts in gtgen.contracts.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WEIGHT_KEYS = (
    "spec_match", "price_score", "moq_fit", "rating_bayes",
    "tenure_years", "location_match", "evidence_confidence",
)


# ------------------------------------------------------------------ reframe / doc_classify
class ReframeItem(BaseModel):
    idx: int
    golden_param: bool
    operator: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    framing_decision: Literal["keep", "translate", "drop"]
    commercial_text: Optional[str] = None
    reason: str = Field(min_length=1)     # D033: persisted as BuyerConstraint.framing_reason

    @model_validator(mode="after")
    def _text_needed(self) -> "ReframeItem":
        if self.framing_decision != "drop" and not (self.commercial_text or "").strip():
            raise ValueError(f"item {self.idx}: commercial_text required unless framing_decision is drop")
        return self


class ReframeOutput(BaseModel):
    items: list[ReframeItem]


class DocClassifyItem(BaseModel):
    idx: int
    is_doc_proof: bool
    doc_name: Optional[str] = None


class DocClassifyOutput(BaseModel):
    items: list[DocClassifyItem]


# ------------------------------------------------------------------ persona
class CategoryTolerance(BaseModel):
    category: str
    multiplier: float = Field(ge=0.0, le=1.5)


class RankingWeightsOut(BaseModel):
    spec_match: float = Field(ge=0.0)
    price_score: float = Field(ge=0.0)
    moq_fit: float = Field(ge=0.0)
    rating_bayes: float = Field(ge=0.0)
    tenure_years: float = Field(ge=0.0)
    location_match: float = Field(ge=0.0)
    evidence_confidence: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _positive_sum(self) -> "RankingWeightsOut":
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("ranking_weights must not all be zero")
        return self


class PersonaOutput(BaseModel):
    company_name: str = Field(min_length=2)
    industry: str = Field(min_length=2)
    city: str = Field(min_length=2)
    style: str = Field(min_length=5)
    tau: float = Field(ge=0.0, le=1.0)
    category_tolerance: list[CategoryTolerance]
    counter_band: float = Field(ge=0.0, le=1.0)
    patience_turns: int = Field(ge=2, le=20)
    max_concessions_per_turn: int = Field(ge=1, le=10)
    ranking_weights: RankingWeightsOut

    @field_validator("tau")
    @classmethod
    def _tau_not_default(cls, v: float) -> float:
        if round(v, 3) in (0.3, 0.5, 0.7):
            raise ValueError("tau must not be 0.3, 0.5 or 0.7; pick another value with 3 decimals")
        return v


class CommercialTermPicks(BaseModel):
    """D049: the persona LLM's picks. Only terms present in the code-supplied bands may be set."""
    model_config = ConfigDict(extra="forbid")
    budget_inr: Optional[float] = None
    qty_vs_moq: Optional[float] = None          # allowed MOQ slack (fraction)
    # D070: kept optional ONLY for compatibility with old model output / fixtures; never requested, validated or used
    # (supplier rating and years of experience are ranking-only). LLMRunners._clean_picks drops them.
    min_supplier_rating: Optional[float] = None
    min_tenure_years: Optional[float] = None
    preferred_states: Optional[list[str]] = None


class PersonaOutputV2(PersonaOutput):
    commercial_terms: CommercialTermPicks


class ToleranceEntry(BaseModel):
    category: str
    value: float            # suggested T_c; the runner clamps it to relaxation.persona_ranges.tolerance


class VoiceOut(BaseModel):
    formality: str = Field(min_length=3)        # e.g. "formal", "semi-formal", "casual"
    verbosity: str = Field(min_length=3)        # e.g. "terse", "balanced", "chatty"
    regional_voice: str = Field(min_length=3)   # e.g. "Gujarati-English business register"


class SalesPersonaOut(BaseModel):
    """The negotiator's own sales personality (D054): warm and keen to close, but truthful.

    D078: no ``name`` field any more - the negotiator never introduces itself by name, so one was never needed."""
    speaking_style: str = Field(min_length=5)
    sales_approach: str = Field(min_length=5)


class PersonaOutputV3(PersonaOutputV2):
    """Persona v3 (D053/D054/D056): relaxation behaviour, voice traits and the negotiator's sales persona.

    The numeric relaxation fields carry no range here on purpose: the runner clamps them to
    ``relaxation.persona_ranges`` (the model may suggest anything).
    """
    max_relaxations: int
    patience_rounds: int
    decision_randomness: float
    tolerance: list[ToleranceEntry]
    voice: VoiceOut
    sales_persona: SalesPersonaOut


# ------------------------------------------------------------------ doc_claim (D051)
class DocClaimItem(BaseModel):
    item_id: int
    claims: bool
    quote: Optional[str] = None


class DocClaimOutput(BaseModel):
    items: list[DocClaimItem]


# ------------------------------------------------------------------ utterances
class UtteranceOutput(BaseModel):
    utterance: str = Field(min_length=1)


class NegotiatorFinalOutput(BaseModel):
    utterance: str = Field(min_length=1)


class NegotiatorReplyOutput(BaseModel):
    utterance: str = Field(min_length=1)
    cited_evidence_ids: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ negotiator_clarify / negotiator_interpret (D044, D045)
class ClarifyOutput(BaseModel):
    utterance: str = Field(min_length=1)
    asked_topics: list[str] = Field(default_factory=list)  # legacy text form (fuzzy-mapped)
    asked_topic_ids: list[int] = Field(default_factory=list)  # v6: indexes into the allowed topic list


class InterpretItem(BaseModel):
    index: int
    response: Literal["ACCEPT", "REFUSE", "COUNTER", "UNCLEAR"]
    counter_value: Optional[str] = None

    @model_validator(mode="after")
    def _counter_value_rule(self) -> "InterpretItem":
        has = bool((self.counter_value or "").strip())
        if self.response == "COUNTER" and not has:
            raise ValueError(f"item {self.index}: counter_value is required when response is COUNTER")
        if self.response != "COUNTER" and self.counter_value is not None:
            raise ValueError(f"item {self.index}: counter_value must be null unless response is COUNTER")
        return self


class InterpretOutput(BaseModel):
    items: list[InterpretItem]


# ------------------------------------------------------------------ negotiator_extract
class ExtractItem(BaseModel):
    attribute: str = Field(min_length=1)
    operator: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    believed_hardness: Literal["HARD", "SOFT"]
    is_doc_proof: bool = False
    source_turn: int


class ExtractOutput(BaseModel):
    items: list[ExtractItem]


# ------------------------------------------------------------------ align / normalise
class AlignMatch(BaseModel):
    candidate_attribute: str
    confidence: float = Field(ge=0.0, le=1.0)


class AlignItem(BaseModel):
    buyer_attribute: str
    matches: list[AlignMatch] = Field(default_factory=list, max_length=3)


class AlignOutput(BaseModel):
    items: list[AlignItem]


class NormaliseItem(BaseModel):
    item_id: int
    value: Optional[str] = None
    unit: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)


class NormaliseOutput(BaseModel):
    items: list[NormaliseItem]


# ------------------------------------------------------------------ reveal_check / claim_check
class RevealOutput(BaseModel):
    revealed_ids: list[str]


class ClaimItem(BaseModel):
    claim: str
    evidence_id: str


class ClaimOutput(BaseModel):
    claims: list[ClaimItem]


# ------------------------------------------------------------------ typed inputs for utterance prompts
class BuyerItemDecision(BaseModel):
    """One already-decided buyer response to a bundle item (decided by deterministic policy)."""
    item: str                              # commercial wording of the constraint
    action: Literal["YES", "NO", "BYPASS", "COUNTER", "PROOF_REQUEST"]
    strength: Literal["must", "prefer"] = "prefer"
    proposed_value: Optional[str] = None
    counter_value: Optional[str] = None
    doc_name: Optional[str] = None
    claimed: Optional[str] = None          # PROOF_REQUEST (D055): what the catalogue claims, in the negotiator's words

    @model_validator(mode="after")
    def _proof_needs_doc(self) -> "BuyerItemDecision":
        if self.action == "PROOF_REQUEST" and not (self.doc_name or "").strip():
            raise ValueError("PROOF_REQUEST needs doc_name")
        return self


class RequirementStatement(BaseModel):
    text: str                              # commercial_text
    strength: Literal["must", "prefer"]


class EvidenceSnippet(BaseModel):
    evidence_id: str
    text: str


class MenuItemView(BaseModel):
    """One change in the chosen menu point, as the negotiator may state it (D053)."""
    attribute: str
    kind: str                              # numeric_widen / drop / doc_to_pending / categorical_add / ...
    from_value: Optional[str] = None
    to_value: Optional[str] = None
    note: Optional[str] = None


class MenuPointView(BaseModel):
    items: list[MenuItemView] = Field(min_length=1)
    extra_options: Optional[int] = None    # how many more suppliers/products become available
    conditions: list[str] = Field(default_factory=list)


class ProofRequestView(BaseModel):
    """What the negotiator needs to acknowledge one buyer proof request (D055). Catalogue-side facts only."""
    attribute: str
    doc_name: str
    evidence_id: str                       # snippet in evidence_json that holds the supplier's claim
