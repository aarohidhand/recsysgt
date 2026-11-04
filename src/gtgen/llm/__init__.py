"""LLM layer: OpenRouter client, disk cache, mock/record modes, prompts and typed runners."""

from .batching import PlannedBatch, plan_batches
from .blocklist import (
    NegotiatorInputError, assert_negotiator_safe, find_government_terms, government_terms, supplier_stats_problem,
    utterance_problem,
)
from .numfmt import format_inr, format_quantity, tidy_numbers
from .claims import listing_text, maybe_claims, verify_quote
from .client import LLMClient, estimate_messages_tokens, estimate_tokens
from .context import (
    ConversationContext, LiveModeTemplateError, MissingConversationContext, forbid_template_in_live, numbers_in,
    numbers_problem,
)
from .exceptions import LLMBudgetError, LLMCallFailed, LLMNetworkError, LLMValidationError, ScenarioSkipped
from .runners import (
    DocClaimRequest, LLMResult, LLMRunners, NormaliseRequest, tightness_hint, build_negotiator_extract_variables,
    build_negotiator_reply_variables, build_negotiator_final_variables, final_text_problem, build_negotiator_clarify_variables, build_negotiator_interpret_variables,
    is_supplier_stat_attribute, opening_problem, tier_for_tau,
)
from .strata import company_similarity, is_near_duplicate, persona_strata, persona_unique, sales_duplicate_reason
from .schemas import (
    BuyerItemDecision, ClarifyOutput, CommercialTermPicks, DocClaimItem, DocClaimOutput, PersonaOutputV2, PersonaOutputV3, MenuItemView, MenuPointView, ProofRequestView, EvidenceSnippet, InterpretItem, InterpretOutput, RequirementStatement,
)

__all__ = [
    "PlannedBatch", "plan_batches", "estimate_tokens", "estimate_messages_tokens",
    "company_similarity", "is_near_duplicate", "persona_strata", "persona_unique", "sales_duplicate_reason",
    "ConversationContext", "LiveModeTemplateError", "MissingConversationContext", "forbid_template_in_live",
    "numbers_in", "numbers_problem", "LLMCallFailed", "PersonaOutputV3", "MenuItemView", "MenuPointView",
    "ProofRequestView",
    "CommercialTermPicks", "DocClaimItem", "DocClaimOutput", "DocClaimRequest", "PersonaOutputV2", "listing_text",
    "maybe_claims", "tightness_hint", "verify_quote",
    "BuyerItemDecision", "ClarifyOutput", "InterpretItem", "InterpretOutput", "build_negotiator_clarify_variables", "build_negotiator_interpret_variables", "EvidenceSnippet", "LLMBudgetError", "LLMClient", "LLMNetworkError", "LLMResult",
    "LLMRunners", "LLMValidationError", "ScenarioSkipped", "NegotiatorInputError", "NormaliseRequest", "RequirementStatement",
    "supplier_stats_problem", "format_inr", "format_quantity", "tidy_numbers", "is_supplier_stat_attribute", "opening_problem",
    "assert_negotiator_safe", "build_negotiator_final_variables", "final_text_problem", "build_negotiator_extract_variables", "build_negotiator_reply_variables", "find_government_terms", "government_terms", "tier_for_tau", "utterance_problem",
]
