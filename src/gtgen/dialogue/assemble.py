"""Assemble a ``GTConversation`` from a finished ``DialogueState`` and write JSONL."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Optional, get_args

from gtgen.contracts import BestAvailable, FinalOutcome, GTConversation
from gtgen.llm import find_government_terms, government_terms
from gtgen.llm.client import active_llm
from gtgen.llm.prompts import PROMPT_NAMES, load_prompt
from gtgen.matching import match_original, perfect_deal
from gtgen.matching.recommend import best_available, negotiated
from gtgen.matching.compliance import cells_by_product, is_inactive
from gtgen.matching.evidence import ref_of
from gtgen.contracts import Compliance
from gtgen.ranking.matrix import build_ranking_matrix

from .loop import DialogueState
from .negotiator import negotiator_seed


def settings_hash(settings: dict) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True, default=str).encode()).hexdigest()


def prompt_versions() -> dict[str, int]:
    return {n: load_prompt(n).version for n in PROMPT_NAMES}


def _dedup(xs: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    return [x for x in xs if not (x in seen or seen.add(x))]


def _truth_best_available(offered, reference, initial_cons, initial_cells, final_cells, settings
                          ) -> BestAvailable:
    """D050: the products the negotiator chose (``offered_product_ids``, in offer order),
    evaluated on the TRUTH side: match_original and unmet constraints over the ORIGINAL truth constraints,
    ``non_compliant`` = a scored FAIL on a negotiated HARD constraint in the FINAL truth state. Nothing is dropped."""
    chosen = list(offered)
    ref = {r.product_id: r for r in reference}
    rows = [ref[p] for p in chosen if p in ref]
    ev = best_available(rows, initial_cons, initial_cells, k=len(rows), settings=settings, final_cells=final_cells)
    by = {i.product_id: i for i in ev.items}
    return reclassify_unknown(BestAvailable(items=[by[r.product_id] for r in rows]), initial_cons, initial_cells)


def _allowed_reasons() -> tuple:
    return get_args(get_args(FinalOutcome.model_fields["best_available_reason"].annotation)[0])


def _belief_role_reasons(ds) -> dict:
    """{belief_ref: zero_pass | low_coverage} from the latest ledger that carries verification_only entries."""
    out: dict = dict(getattr(ds, "role_reasons", None) or {})
    for rec in ds.constraint_attempts:
        for ref, e in (rec.get("attempts") or {}).items():
            if isinstance(e, dict) and e.get("status") == "verification_only":
                out[ref] = e.get("reason", "low_coverage")
    return out


def reclassify_unknown(best: BestAvailable, constraints, cells) -> BestAvailable:
    """D069 / D050: only an OBSERVED scored FAIL is 'unmet'. A negotiated constraint whose cell is UNKNOWN (or missing)
    moves from ``unmet_constraints`` to ``verification_required`` (to confirm). Idempotent; nothing else changes
    (match_original is computed by matching.recommend)."""
    cells = list(cells)
    by = cells_by_product(cells)
    cons = negotiated(constraints)
    out = []
    for it in best.items:
        fails = {c.attribute for c in cons
                 if (cell := by.get(it.product_id, {}).get(ref_of(c))) is not None
                 and cell.scored and cell.status == Compliance.FAIL}
        unmet = [a for a in it.unmet_constraints if a in fails]
        unknown = [a for a in it.unmet_constraints if a not in fails]
        out.append(it.model_copy(update={"unmet_constraints": unmet,
                                         "verification_required": _dedup(list(it.verification_required) + unknown)}))
    return BestAvailable(items=out)


def with_proof_docs(rows, proofs):
    """D055: add the documents the buyer asked for to each candidate's ``pending_verification_docs``. Nothing else in a
    row changes (p_feasible, utility, score, p_recommend, match_original, rank, eligible are untouched)."""
    by_pid: dict[str, list[str]] = {}
    for p in proofs:
        by_pid.setdefault(p.product_id, []).append(p.doc_name)
    if not by_pid:
        return list(rows)
    return [r.model_copy(update={"pending_verification_docs": sorted(set(r.pending_verification_docs)
                                                                      | set(by_pid.get(r.product_id, ())))})
            for r in rows]


def assemble_conversation(dialogue_state: DialogueState, run_id: str, settings: dict) -> GTConversation:
    ds = dialogue_state
    brief, persona, profile = ds.brief, ds.persona, ds.buyer_profile
    # D052: reference ranking from the FINAL agreed truth state, the initial one from the state before any concession
    initial_cons = ds.initial_constraints or [c for c in brief.constraints if not is_inactive(c)]
    initial_cells = ds.initial_cells or ds.truth_cells
    final_cons = ds.final_constraints or initial_cons
    rank_kw = dict(quantity=brief.quantity, budget_inr=brief.budget_inr, weights=persona.ranking_weights,
                   buyer_city=profile.city, settings=settings, original_constraints=initial_cons,
                   original_cells=initial_cells)
    reference = with_proof_docs(build_ranking_matrix(final_cons, ds.catalogue.candidates, ds.truth_cells, **rank_kw),
                                ds.proof_requests)
    reference_initial = build_ranking_matrix(initial_cons, ds.catalogue.candidates, initial_cells, **rank_kw)
    negotiator_ranking = list(ds.final_ranking or [])          # D050: both outcomes carry the belief-side ranking
    pending = _dedup([c.pending_doc for c in ds.truth_cells if c.pending_doc] + [p.doc_name for p in ds.proof_requests])

    best: Optional[BestAvailable] = None
    if ds.outcome == "BEST_AVAILABLE":
        best = _truth_best_available(ds.offered_product_ids, reference, initial_cons, initial_cells, ds.truth_cells,
                                     settings)
    reason = ds.best_available_reason if ds.outcome == "BEST_AVAILABLE" else None
    extra_flags: list[str] = []
    if reason is not None and reason not in _allowed_reasons():
        extra_flags.append(f"best_available_reason:{reason}")      # contract Literal lacks it: keep the info as a qa flag
        reason = "no_feasible"
    final = FinalOutcome(
        outcome=ds.outcome, best_available=best,
        best_available_reason=reason,
        reference_ranking=reference, reference_ranking_initial=reference_initial,
        negotiator_ranking=negotiator_ranking,
        accepted_relaxations=list(ds.accepted_relaxations), pending_verification_docs=pending,
        proof_requests=list(ds.proof_requests),
        compliance_detail_truth=list(ds.truth_cells), compliance_detail_belief=list(ds.belief_cells))

    top1_match: Optional[float] = None
    top1_perfect: Optional[bool] = None
    if negotiator_ranking:                  # D050: filled for both outcomes, truth-side values
        top1 = best.items[0].product_id if best is not None and best.items else negotiator_ranking[0].product_id
        mo = match_original(initial_cons, initial_cells, settings,
                            product_ids=[c.product_id for c in ds.catalogue.candidates])
        top1_match = mo.get(top1)
        top1_perfect = perfect_deal(top1, final_cons, ds.truth_cells)

    terms = government_terms(settings)
    flags = list(ds.qa_flags) + extra_flags
    if best is not None:                    # never silently dropped, and visible to the auditor
        flags += [f"best_available_non_compliant:{i.product_id}" for i in best.items if i.non_compliant]
    for t in ds.turns:
        for term in find_government_terms(t.utterance, terms):
            flags.append(f"government_term:{t.turn_idx}:{term}")

    llm_backend, llm_model = active_llm(settings)   # D108: what really ran (GTGEN_LLM_BACKEND), not file defaults
    provenance = {
        "run_id": run_id,
        "scenario_id": brief.scenario_id,
        "persona_seed": persona.persona_seed,
        "negotiator_seed": negotiator_seed(ds.catalogue.scenario_id),
        "tau_band": list(persona.tau_band) if persona.tau_band else None,
        "settings_hash": settings_hash(settings),
        "prompt_versions": prompt_versions(),
        "llm_model": llm_model,
        "llm_backend": llm_backend,
        "persona_llm_call_id": persona.llm_call_id,
        "profile_llm_call_id": profile.llm_call_id,
        "alignment_llm_call_ids": _dedup(a.llm_call_id for a in ds.alignments),
        "n_candidates": len(ds.catalogue.candidates),
        "llm_calls": ds.llm_provenance or None,        # D054: live / cache_hit / fixture counts of this scenario
        "claim_cache_stats": dict(ds.claim_cache_stats or {}),     # D071 dialogue doc_claim: pairs / hits / sent / dedup
        "doc_claim_stats": dict((brief.scenario_meta or {}).get("doc_claim_stats") or {}),   # table-stage doc_claim stats
        "belief_role_reasons": _belief_role_reasons(ds),
        "relaxation": {"truth_bounds_final": ds.truth_bounds, "n_rounds": len(ds.relaxation_analysis)},
    }
    # D076: the pool's states (candidate order) so eval can replay state decisions exactly (GT-side metadata)
    meta = dict(brief.scenario_meta or {})
    meta["pool_states"] = [(c.state if c.state and str(c.state).strip() else None) for c in ds.catalogue.candidates]
    out_brief = brief.model_copy(update={"scenario_meta": meta})
    return GTConversation(
        scenario_id=brief.scenario_id, run_id=run_id, bid_ref=brief.bid_ref, buyer_profile=profile,
        persona=persona, commercial_brief=out_brief, turns=list(ds.turns), final=final,
        provenance=provenance, top1_match_original=top1_match, top1_perfect_deal=top1_perfect,
        relaxation_analysis=list(ds.relaxation_analysis), qa_flags=flags)


def conversation_path(run_id: str, settings: dict) -> Path:
    """Return the checkpoint path for a run's conversations."""
    return Path(settings["paths"]["out"]) / run_id / "conversations.jsonl"


def append_conversation(conv: GTConversation, run_id: str, settings: dict) -> Path:
    """Append one finished conversation and fsync it immediately (D086).

    The pilot calls this as each scenario finishes, so a killed/timed-out process keeps every
    conversation that already completed.  ``write_conversations`` remains the bulk/rewrite API.
    """
    path = conversation_path(run_id, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(conv.model_dump_json() + "\n")
        f.flush()
        os.fsync(f.fileno())
    return path


def write_conversations(conversations: Iterable[GTConversation], run_id: str, settings: dict) -> Path:
    """Write ``out/gt/<run_id>/conversations.jsonl`` (one conversation per line)."""
    path = conversation_path(run_id, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for conv in conversations:
            f.write(conv.model_dump_json() + "\n")
    return path
