"""Shared interface contracts. OWNER: main session (coordinator).

Subagents import from here and never edit this file; request changes via the coordinator.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------- enums
class Hardness(str, Enum):
    HARD = "HARD"
    SOFT = "SOFT"


class Compliance(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"  # claimed, proof document not seen; no rank penalty


class Speaker(str, Enum):
    BUYER = "BUYER"
    NEGOTIATOR = "NEGOTIATOR"


class Action(str, Enum):
    # buyer actions
    YES = "YES"
    NO = "NO"
    BYPASS = "BYPASS"
    COUNTER = "COUNTER"
    MIXED = "MIXED"
    REVEAL = "REVEAL"               # D044: buyer states further requirements (answer to CLARIFY / touched constraints)
    WRAP_UP = "WRAP_UP"             # D050: buyer, out of patience or turns, asks for the negotiator's best available offer (public turn)
    # negotiator actions
    ACKNOWLEDGE = "ACKNOWLEDGE"     # D053/D055: negotiator confirms a buyer BYPASS / proof request and names the document the supplier must provide
    OPEN_REQUEST = "OPEN_REQUEST"   # buyer opening message
    CLARIFY = "CLARIFY"
    PROPOSE_RELAXATION = "PROPOSE_RELAXATION"
    RECOMMEND = "RECOMMEND"
    BEST_AVAILABLE = "BEST_AVAILABLE"             # D050: negotiator recommends the best available candidates (top-k by match_original)


class Tier(str, Enum):
    LENIENT = "lenient"
    MEDIUM = "medium"
    MEDIUM_STRICT = "medium_strict"
    STRICT = "strict"


class FramingDecision(str, Enum):
    KEEP = "keep"
    TRANSLATE = "translate"   # government wording -> commercial equivalent
    DROP = "drop"             # government-only (EMD, MSE preference, bid security...)
    MERGE = "merge"           # D046: clause-conformity member folded into one doc-proof constraint (ids kept in merged_from)


class ScenarioRole(str, Enum):
    """D048: what a constraint does in a scenario. Hidden from the negotiator."""
    NEGOTIATED = "negotiated"                  # scored; evidence coverage in the bid pool >= scenario.negotiated_min_coverage (SOFT: always)
    VERIFICATION_ONLY = "verification_only"    # HARD or SOFT (not synthetic), coverage below threshold: voiced, listed in verification_required; MISSING evidence is not scored, an observed FAIL still counts (D048 amended)
    DROPPED = "dropped"                        # government-only


# ---------------------------------------------------------------- buyer side (hidden from negotiator)
class BuyerConstraint(BaseModel):
    """One row of final_constraints.csv after canonical filtering + commercial reframing,
    or a simulator-defined commercial term (provenance "synthetic_commercial", D049)."""
    constraint_id: str
    bid_id: str
    golden_param: Optional[bool]           # verbatim from the data; None ONLY for provenance "synthetic_commercial" (never invented)
    hardness: Hardness                     # derived from golden_param via config mapping only (synthetic terms are SOFT by definition)
    provenance: Literal["gem", "synthetic_commercial"] = "gem"   # D049
    scenario_role: Optional[ScenarioRole] = None                 # D048; None until the scenario-construction step has run
    evidence_coverage: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # D048: share of the bid pool with non-UNKNOWN evidence
    role_reason: Optional[str] = None            # D069: why scenario_role is verification_only: 'low_coverage' (D048) | 'zero_pass' (no candidate PASSes it); None otherwise
    merged_from: list[str] = Field(default_factory=list)         # D046: original constraint ids folded into this one
    attribute: str
    spec_name: Optional[str] = None
    buyer_value: Optional[str] = None
    operator: Optional[str] = None
    unit: Optional[str] = None
    allowed_values: Optional[list[str]] = None
    allowed_values_raw: Optional[str] = None  # D019: verbatim cell when JSON parse failed (allowed_values is then None)
    allows_higher: Optional[bool] = None
    constraint_category: Optional[str] = None
    constraint_type: Optional[str] = None
    scope: Optional[str] = None
    condition: Optional[str] = None        # when populated, encodes when this constraint applies
    is_doc_proof: bool = False             # LLM classification: satisfied by a certificate/document
    doc_name: Optional[str] = None         # name of the required certificate/document
    framing_decision: FramingDecision = FramingDecision.KEEP
    framing_reason: Optional[str] = None   # D033: LLM-generated reason for the framing decision
    commercial_text: Optional[str] = None  # how a private company would state it
    raw_text: Optional[str] = None
    evidence_anchor: Optional[str] = None
    llm_call_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _golden_param_is_data(self) -> "BuyerConstraint":
        """Rule 1.1: golden_param is a data field. GEM rows must carry it; synthetic terms must not invent one."""
        if self.provenance == "gem" and self.golden_param is None:
            raise ValueError("golden_param is required for provenance 'gem'")
        if self.provenance == "synthetic_commercial":
            if self.golden_param is not None:
                raise ValueError("synthetic_commercial constraints have no golden_param (must be None)")
            if self.hardness != Hardness.SOFT:
                raise ValueError("synthetic_commercial constraints are SOFT by definition (D049)")
        return self


class BuyerProfile(BaseModel):
    """LLM-generated fictional company identity. Visible in utterances."""
    company_name: str
    industry: str
    city: str
    style: str                             # speaking style description
    llm_call_id: str


class Persona(BaseModel):
    """Behavioural parameters that drive the deterministic buyer policy."""
    persona_seed: int
    tau: float = Field(ge=0.0, le=1.0)     # hardness threshold, LLM-generated within tau_band
    tau_band: Optional[tuple[float, float]] = None  # D036: [lo, hi) assigned by seeded RNG; None = free choice
    tier: Tier                             # deterministic binning of tau
    category_tolerance: dict[str, float]   # w_category multipliers in [0, 1.5]
    counter_band: float = Field(ge=0.0, le=1.0)
    patience_turns: int = Field(ge=2, le=20)
    max_concessions_per_turn: int = Field(ge=1, le=10, default=2)
    ranking_weights: dict[str, float]      # feature -> weight, normalised to sum 1
    commercial_terms: dict = Field(default_factory=dict)  # D049: values the persona LLM chose for the synthetic terms, inside code-derived bands
    max_relaxations: Optional[int] = Field(default=None, ge=0, le=10)         # D053 §11: accepted changes the buyer will make in total (persona v3)
    patience_rounds: Optional[int] = Field(default=None, ge=1, le=10)         # D053 §11: rejected proposals tolerated
    decision_randomness: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # D053 §11: noise in the accept probability
    tolerance: dict[str, float] = Field(default_factory=dict)                 # D053 §11: T_c per constraint category (max relaxation depth)
    strata: dict = Field(default_factory=dict)  # D060: {trait: {"index": k, "of": n, "band": [lo, hi]}} assigned by code before the LLM call (tau, tolerance, patience_rounds, decision_randomness, max_relaxations)
    llm_call_id: str


# ---------------------------------------------------------------- scenario brief
class ScenarioBrief(BaseModel):
    """Commercial brief for one negotiation scenario. Replaces raw constraint list."""
    scenario_id: str
    bid_ref: str                           # hidden; for splits/leakage control only
    quantity: float
    quantity_unit: str
    budget_inr: Optional[float] = None     # from price/budget constraint if present; else (D049) the persona's budget_inr term
    constraints: list[BuyerConstraint]
    scenario_meta: dict = Field(default_factory=dict)  # D048/D049: coverage method, commercial bands, tightness_target, initial_feasible, tightening_steps


# ---------------------------------------------------------------- candidate side (negotiator may see)
class CandidateConstraint(BaseModel):
    """One row of product_constraints.csv (candidate-side evidence)."""
    product_id: str
    supplier_id: str
    constraint_id: str
    category: Optional[str] = None         # broad constraint category for matching
    attribute: str
    spec_name: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[str] = None
    allowed_values: Optional[list[str]] = None
    allowed_values_raw: Optional[str] = None  # D019: verbatim cell when JSON parse failed
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    unit: Optional[str] = None
    raw_text: Optional[str] = None
    raw_text_verified: Optional[bool] = None
    confidence: Optional[float] = None
    source_field: Optional[str] = None


class CandidateView(BaseModel):
    """Catalogue record visible to the negotiator. NO bid-derived fields."""
    product_id: str
    supplier_id: str
    product_name: str
    category_name: Optional[str] = None
    price: Optional[float] = None
    price_formatted: Optional[str] = None
    moq: Optional[float] = None
    moq_type: Optional[str] = None
    description: Optional[str] = None
    specifications: Optional[dict] = None
    specifications_raw: Optional[str] = None  # D019: verbatim cell when JSON parse failed (specifications is then None)
    brochure_text: Optional[str] = None
    supplier_name: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    rating: Optional[float] = None
    rating_count: Optional[int] = None
    member_since: Optional[str] = None
    constraints: list[CandidateConstraint] = Field(default_factory=list)


class CatalogueView(BaseModel):
    scenario_id: str
    candidates: list[CandidateView]


# ---------------------------------------------------------------- transcript (negotiator's view)
class TranscriptTurn(BaseModel):
    """Minimal turn visible to the Negotiator — no hidden state."""
    turn_idx: int
    speaker: Speaker
    utterance: str


class TranscriptView(BaseModel):
    """The only conversation type the Negotiator accepts."""
    scenario_id: str
    turns: list[TranscriptTurn]


# ---------------------------------------------------------------- matching
class AttributeAlignment(BaseModel):
    """LLM-produced mapping: buyer attribute name <-> candidate spec name. Per bid, cached."""
    bid_id: str
    buyer_attribute: str
    candidate_attribute: str
    confidence: float
    llm_call_id: str


class NormalisedValue(BaseModel):
    """LLM-produced normalisation of free-text evidence into a comparable value+unit."""
    source_text: str
    value: str
    unit: Optional[str] = None
    confidence: float
    llm_call_id: str


class DocClaim(BaseModel):
    """D051: does a listing claim the required document/standard? LLM extraction WITH a quote, then a
    deterministic check that the quote occurs in the listing text. An unverified quote counts as no claim."""
    product_id: str
    constraint_ref: str            # constraint_id (truth) or belief_id (negotiator)
    claims: bool                   # True only if quote_verified
    quote: Optional[str] = None
    quote_verified: bool = False   # quote found verbatim (case/whitespace-normalised) in the listing text
    llm_call_id: str

    @model_validator(mode="after")
    def _claim_needs_verified_quote(self) -> "DocClaim":
        if self.claims and not (self.quote and self.quote_verified):
            raise ValueError("a claim requires a quote that was verified against the listing text")
        return self


class BelievedConstraint(BaseModel):
    """Negotiator's understanding of a buyer requirement, extracted from the transcript only."""
    belief_id: str
    attribute: str
    operator: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    believed_hardness: Hardness
    is_doc_proof: bool = False
    source_turn: int
    matched_constraint_id: Optional[str] = None  # filled by GT assembler only, never shown to negotiator


class ComplianceCell(BaseModel):
    product_id: str
    constraint_ref: str            # constraint_id (truth) or belief_id (negotiator)
    status: Compliance
    evidence: Optional[str] = None
    evidence_confidence: Optional[float] = None
    pending_doc: Optional[str] = None
    scored: bool = True            # D048: False for verification_only constraints (kept for audit, never blocks, not in p_feasible)


class RelaxationOption(BaseModel):
    constraint_ref: str
    kind: Literal["numeric_widen", "categorical_extend", "drop", "doc_to_pending"]
    from_value: Optional[str] = None
    to_value: Optional[str] = None
    normalised_cost: float = Field(ge=0.0, le=1.0)
    candidates_gained: int
    feasible_after: int
    gained_product_ids: list[str] = Field(default_factory=list)
    truth_gain: Optional[int] = None                    # D056/item 1: role-aware gain from the TRUTH state (the shared gain function), set on accepted changes
    negotiator_estimated_gain: Optional[int] = None     # D056/item 1: the same gain from the negotiator's BELIEF state; eval: gain_estimate_error


class RelaxationBundle(BaseModel):
    """Multiple relaxation options proposed together in one negotiator turn."""
    items: list[RelaxationOption] = Field(min_length=1)
    joint_gain: int = 0                    # D025: infeasible candidates made feasible by all items together
    feasible_after: int = 0
    gained_product_ids: list[str] = Field(default_factory=list)
    total_normalised_cost: float = 0.0


class ItemResponse(BaseModel):
    """Buyer's response to one item in a RelaxationBundle or one D055 proof request.
    D059: ``constraint_ref`` is ALWAYS the buyer's TRUTH constraint_id (this record is buyer-side GT, never shown to the
    Negotiator); ``belief_ref`` carries the Negotiator's belief id for the same item when the item came from a proposal
    (None for a proof request the Negotiator holds no belief for)."""
    constraint_ref: str
    belief_ref: Optional[str] = None
    action: Action
    counter_value: Optional[str] = None


class ProofRequest(BaseModel):
    """D055: the buyer asks for proof of an attribute the catalogue claims. Pure verification: never changes a cell status, p_feasible, utility,
    score, p_recommend, match_original, perfect_deal or rank."""
    turn_idx: int
    product_id: str
    constraint_id: str
    attribute: str
    doc_name: str


class RelaxationAnalysisRow(BaseModel):
    """D056: one negotiator-turn iteration of the relaxation loop (a GT table row)."""
    turn_idx: int
    iteration: int
    front_points: list[dict] = Field(default_factory=list)       # menu/front points considered (rule -> level, supplier_count, cost)
    chosen_point: Optional[dict] = None
    chosen_action: Optional[str] = None                          # PROPOSE_RELAXATION / RECOMMEND / WRAP_UP / BEST_AVAILABLE
    bounds_before_truth: dict = Field(default_factory=dict)
    bounds_after_truth: dict = Field(default_factory=dict)
    bounds_before_belief: dict = Field(default_factory=dict)
    bounds_after_belief: dict = Field(default_factory=dict)
    verified_before_truth: int = 0
    verified_after_truth: int = 0
    verified_before_belief: int = 0
    verified_after_belief: int = 0
    buyer_decision: Optional[dict] = None                        # per-item action (YES/NO/COUNTER/BYPASS) from the buyer's actual decision
    agrees_with_reference: Optional[bool] = None
    constraint_attempts: dict = Field(default_factory=dict)      # D072: per-constraint relaxation-attempt ledger (doc_immutable / verification_only / hard_not_relaxed / accepted / refused_by_buyer / countered / proposed / non_relaxable / no_reachable_gain / pending_budget_exhausted) + loop_exhausted


class Proposal(BaseModel):
    proposer: Speaker
    option: Optional[RelaxationOption] = None
    bundle: Optional[RelaxationBundle] = None


# ---------------------------------------------------------------- ranking
class RankingRow(BaseModel):
    product_id: str
    supplier_id: str
    hard_pass: int
    hard_fail: int
    hard_unknown: int
    soft_pass: int
    soft_fail: int
    soft_unknown: int
    pending_verification_docs: list[str]
    spec_match: float
    price_score: float
    price_vs_budget: Optional[float]
    moq_fit: float
    rating_bayes: float
    rating_count: int
    tenure_years: Optional[float]
    location_match: float
    evidence_confidence: float
    p_feasible: float
    utility: float
    score: float
    p_recommend: float
    rank: int
    eligible: bool
    match_original: Optional[float] = None   # D050: weighted share of the ORIGINAL negotiated constraints this candidate satisfies (HARD 2, SOFT 1, verification_only excluded)
    verification_required: list[str] = Field(default_factory=list)  # D048: attributes of verification_only HARD constraints (and HARD doc-proof cells without a claim) to confirm with the supplier


# ---------------------------------------------------------------- conversation GT
class Turn(BaseModel):
    turn_idx: int
    speaker: Speaker
    action: Action
    utterance: str
    proposal: Optional[Proposal] = None
    proposal_outcome: Optional[Literal["ACCEPTED", "REJECTED", "COUNTERED", "BYPASSED", "PARTIAL"]] = None
    counter_value: Optional[str] = None
    item_responses: list[ItemResponse] = Field(default_factory=list)
    proof_requests: list[ProofRequest] = Field(default_factory=list)   # D055
    revealed_constraint_ids: list[str] = Field(default_factory=list)
    buyer_truth_state_after: list[BuyerConstraint] = Field(default_factory=list)
    negotiator_belief_state: list[BelievedConstraint] = Field(default_factory=list)
    feasible_count_truth: int = 0
    feasible_count_belief: int = 0
    llm_call_ids: list[str] = Field(default_factory=list)


class BestAvailableItem(BaseModel):
    """D050: one of the top-k candidates offered when no candidate satisfies everything."""
    product_id: str
    supplier_id: str
    match_original: float                                   # see RankingRow.match_original
    unmet_constraints: list[str] = Field(default_factory=list)       # attributes of negotiated constraints this candidate does not meet
    verification_required: list[str] = Field(default_factory=list)
    non_compliant: bool = False                             # True iff it has a negotiated HARD FAIL; counts in hard-violation metrics


class BestAvailable(BaseModel):
    items: list[BestAvailableItem]                          # top-k by match_original (settings recommendation.best_available_k)


class FinalOutcome(BaseModel):
    outcome: Literal["RECOMMENDED", "BEST_AVAILABLE"]   # D050: the negotiator always recommends something
    best_available: Optional[BestAvailable] = None          # D050: set iff outcome == BEST_AVAILABLE
    best_available_reason: Optional[Literal["no_feasible", "patience_exhausted", "nothing_verified"]] = None
    reference_ranking: list[RankingRow]          # D052: computed from the FINAL agreed buyer truth state (accepted relaxations applied)
    negotiator_ranking: list[RankingRow]         # computed from negotiator belief state
    reference_ranking_initial: list[RankingRow] = Field(default_factory=list)  # D052: truth ranking before any concession
    accepted_relaxations: list[RelaxationOption]
    pending_verification_docs: list[str] = Field(default_factory=list)
    proof_requests: list[ProofRequest] = Field(default_factory=list)   # D055: aggregated; the docs are also in pending_verification_docs
    compliance_detail_truth: list[ComplianceCell] = Field(default_factory=list)
    compliance_detail_belief: list[ComplianceCell] = Field(default_factory=list)


class GTConversation(BaseModel):
    scenario_id: str
    run_id: str
    bid_ref: str                                  # hidden; for splits/leakage control only
    buyer_profile: BuyerProfile
    persona: Persona
    commercial_brief: ScenarioBrief
    turns: list[Turn]
    final: FinalOutcome
    provenance: dict
    top1_match_original: Optional[float] = None             # D050: match_original of the top-1 recommendation
    top1_perfect_deal: Optional[bool] = None                # D050: top-1 satisfies every negotiated constraint in the FINAL agreed truth state
    relaxation_analysis: list[RelaxationAnalysisRow] = Field(default_factory=list)   # D056
    qa_flags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- negotiator under test (D030)
_NEGOTIATOR_ACTIONS = {Action.CLARIFY, Action.PROPOSE_RELAXATION, Action.RECOMMEND,
                       Action.BEST_AVAILABLE, Action.ACKNOWLEDGE}


class NegotiatorStep(BaseModel):
    """One negotiator turn produced from (TranscriptView, CatalogueView) only."""
    beliefs: list[BelievedConstraint] = Field(default_factory=list)
    action: Action
    proposal: Optional[Proposal] = None
    ranking: Optional[list[RankingRow]] = None   # full ranking by score (rank 1 = highest score); metrics such as NDCG/tau read it
    offered_product_ids: list[str] = Field(default_factory=list)  # D050: the BEST_AVAILABLE offer (top-k by match_original), in offer order
    utterance: str
    llm_call_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "NegotiatorStep":
        if self.action not in _NEGOTIATOR_ACTIONS:
            raise ValueError(f"negotiator action must be one of {sorted(a.value for a in _NEGOTIATOR_ACTIONS)}")
        if (self.proposal is not None) != (self.action == Action.PROPOSE_RELAXATION):
            raise ValueError("proposal must be set iff action is PROPOSE_RELAXATION")
        if self.proposal is not None and self.proposal.proposer != Speaker.NEGOTIATOR:
            raise ValueError("proposal.proposer must be NEGOTIATOR")
        if (self.ranking is not None) != (self.action in (Action.RECOMMEND, Action.BEST_AVAILABLE)):
            raise ValueError("ranking must be set iff action is RECOMMEND or BEST_AVAILABLE")
        if (len(self.offered_product_ids) > 0) != (self.action == Action.BEST_AVAILABLE):
            raise ValueError("offered_product_ids must be set iff action is BEST_AVAILABLE")
        return self


@runtime_checkable
class NegotiatorUnderTest(Protocol):
    """Any negotiator (GT or system under evaluation). The signature is the information barrier."""

    def step(self, transcript: TranscriptView, catalogue: CatalogueView) -> NegotiatorStep: ...
