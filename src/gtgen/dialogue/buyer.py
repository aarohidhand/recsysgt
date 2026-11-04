"""Buyer simulator: holds the truth state; the D053 decision code decides, the LLM only verbalises.

Per proposal item (``matching.buyer_decide``, spec 11.3 as gtgen deviates, D053 item 6):
    unmatched belief (the buyer never stated it)          -> YES, no state change (not counted against max_relaxations)
    doc-proof constraint                                  -> BYPASS (verification list only)
    HARD, or not a relaxable rule (verification_only, non-numeric) -> NO (CLAUDE.md 1.6)
    new rules accepted would exceed persona.max_relaxations -> NO
    level r > tolerance T_c                                -> COUNTER at T_c (every tier, cur <= T_c < r), else NO
    otherwise                                              -> YES with probability sigmoid(...) from the buyer rng
The proposed VALUE is mapped to a level of the buyer's own rule through the relative change it asks for. A proposal the
buyer's bounds already exclude (a rule it refused or capped earlier) is answered NO without a draw and without a new bound.
YES -> bound fixed + Change record; NO -> max 0; COUNTER -> max k; BYPASS -> verification list only.

D055: after the decision, a seeded draw from the SAME rng per YES item (sorted by constraint id, at most
``proof_requests.max_per_turn``) may turn into a proof request for a candidate the catalogue claims; it never changes a
cell, a bound or any ranking input.

One ``random.Random`` per conversation: ``buyer_rng(persona.persona_seed)``.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from gtgen.contracts import (
    Action, BuyerConstraint, BuyerProfile, Compliance, Hardness, ItemResponse, Persona, ProofRequest,
    RelaxationBundle, RelaxationOption, ScenarioBrief, TranscriptView,
)
from gtgen.llm import BuyerItemDecision, ConversationContext, RequirementStatement
from gtgen.llm.numfmt import tidy_numbers
from gtgen.matching import (
    BuyerParams, ItemOutcome, added_states, ProposalItem, buyer_decide, buyer_rng, level_target, params_from_persona,
    role_aware_gain,
)
from gtgen.matching.bounds import domain_levels

from ._proof import proof_doc_name
from ._relax import change_fraction, level_ceil, scale_value, state_level_from_text, state_phrase
from ._reveal import dedupe_areas, priority_order, reveal_candidates, reveal_config, select_opening, touched
from ._truth import TruthState
from ._util import guarded_call

STUB_OPENING = "We are looking to purchase products meeting our specifications."
STUB_REVEAL = "Let me come back to you on the remaining details."
STUB_REPLY = "Thank you, let me consider that."

_OUTCOME = {Action.YES: "ACCEPTED", Action.NO: "REJECTED", Action.BYPASS: "BYPASSED",
            Action.COUNTER: "COUNTERED"}


@dataclass
class BuyerResponse:
    action: Action
    outcome: str
    counter_value: Optional[str]
    responses: list[ItemResponse]                       # one per proposal item (+ one BYPASS per proof request)
    proof_requests: list[ProofRequest]
    accepted: list[RelaxationOption]                    # YES items with truth_gain set
    decision: dict[str, dict]                           # belief ref -> record (RelaxationAnalysisRow.buyer_decision)
    utterance: str
    llm_call_ids: list[str]
    yes: bool                                           # D100: any YES / COUNTER / BYPASS (progress); only ALL-NO burns patience
    errors: list[str] = field(default_factory=list)


def _plain_qty(q: Optional[float]) -> Optional[float]:
    """Whole counts as ints (500.0 -> 500); a prompt never shows a float artefact."""
    if q is None:
        return None
    return int(round(q)) if abs(q - round(q)) < 1e-9 or abs(q) >= 10 else round(float(q), 2)


def _plain_money(v: Optional[float]) -> Optional[int]:
    """Money as a whole-rupee amount (215335.9375 -> 215336)."""
    return None if v is None else int(round(v))


class BuyerSimulator:
    def __init__(self, brief: ScenarioBrief, persona: Persona, profile: BuyerProfile,
                 runners: Any, settings: dict, product_category: Optional[str] = None, *,
                 truth: Optional[TruthState] = None, rng: Optional[random.Random] = None) -> None:
        self.brief = brief
        self.persona = persona
        self.profile = profile
        self.runners = runners
        self.settings = settings
        self.product_category = product_category or "products"
        self.reveal_cfg = reveal_config(settings)          # D044: every key required
        self.truth = truth
        self.rng = rng if rng is not None else buyer_rng(persona.persona_seed)     # ONE stream per conversation
        self.params: BuyerParams = params_from_persona(persona, truth.cfg if truth is not None else None)
        self.proof_cfg = settings.get("proof_requests", {}) or {}
        self.revealed_ids: list[str] = []                  # verified by reveal_check only, never assumed
        self.proof_log: list[ProofRequest] = []
        self._qa_flags: list[str] = []
        self.ctx0 = ConversationContext(brief.scenario_id, persona.persona_seed)

    # ------------------------------------------------------------ helpers
    def drain_qa_flags(self) -> list[str]:
        out, self._qa_flags = self._qa_flags, []
        return out

    def ctx(self, turn_idx: int) -> ConversationContext:
        return self.ctx0.at_turn(turn_idx)

    @staticmethod
    def _spoken(rule, level: Optional[int], value: Optional[str]) -> Optional[str]:
        """Value shown to the buyer-reply verbaliser: numeric values as they are; a supplier-state widening (D076) as a
        plain-language phrase ('also consider suppliers from Rajasthan and Maharashtra'), never a number or an internal name."""
        if rule is None or rule.mode != "set" or level is None:
            return value
        added = added_states(rule, level)
        if not added:                       # D082: a level-0 counter keeps the original set, not "anywhere"
            return value
        return state_phrase(added, level == rule.top)

    @staticmethod
    def _label(c: BuyerConstraint) -> str:
        return tidy_numbers(c.commercial_text) or c.attribute      # D070: no "500.0 units" / "43.38 units" in prompts

    def already_stated(self) -> list[str]:
        """The buyer's own wording of the requirements it has already said (D054)."""
        done = set(self.revealed_ids)
        return [self._label(c) for c in self.brief.constraints if c.constraint_id in done]

    # ------------------------------------------------------------ opening / progressive reveal (D044)
    @staticmethod
    def _requirement(c: BuyerConstraint) -> RequirementStatement:
        return RequirementStatement(text=BuyerSimulator._label(c),
                                    strength="must" if c.hardness == Hardness.HARD else "prefer")

    def _record_reveals(self, ids: list[str]) -> list[str]:
        """Add confirmed ids to ``revealed_ids``; returns the ones that are new."""
        new = [i for i in dict.fromkeys(ids) if i not in self.revealed_ids]
        self.revealed_ids.extend(new)
        return new

    def unrevealed(self) -> list[BuyerConstraint]:
        """Not-yet-revealed, non-DROP constraints in priority order."""
        done = set(self.revealed_ids)
        return [c for c in priority_order(self.brief.constraints, self.reveal_cfg) if c.constraint_id not in done]

    def opening(self, seed: int, turn_idx: int = 0) -> tuple[str, list[str], list[str]]:
        """(utterance, revealed_constraint_ids, llm_call_ids).

        Progressive reveal on: only ``select_opening(...)`` is handed to the LLM; off: every constraint (D044).
        ``revealed`` is what ``reveal_check`` found in the produced text, not what was selected."""
        if self.reveal_cfg.enabled:
            stated = select_opening(self.brief.constraints, self.persona.persona_seed, self.reveal_cfg)
        else:
            stated = reveal_candidates(self.brief.constraints)
        reqs = [self._requirement(c) for c in stated]
        ctx = self.ctx(turn_idx)
        utt, ids = guarded_call(
            self.runners.buyer_opening, "buyer_opening", STUB_OPENING, self._qa_flags,
            self.profile, self.product_category,
            _plain_qty(self.brief.quantity), self.brief.quantity_unit,
            _plain_money(self.brief.budget_inr), reqs, seed=seed, ctx=ctx, persona=self.persona)
        revealed: list[str] = []
        if self.brief.constraints:
            revealed, rids = guarded_call(
                self.runners.reveal_check, "reveal_check", [], self._qa_flags,
                utt, reveal_candidates(self.brief.constraints), seed=seed, ctx=ctx)
            ids += rids
        if self.reveal_cfg.enabled and len(revealed) > self.reveal_cfg.opening_max:
            self._qa_flags.append("opening_over_max")           # D044: the opening may state at most opening_max
        return utt, self._record_reveals(list(revealed)), ids

    def stated(self) -> list[BuyerConstraint]:
        """Constraints the buyer has already said (verified by ``reveal_check``), in priority order."""
        done = set(self.revealed_ids)
        return [c for c in priority_order(self.brief.constraints, self.reveal_cfg) if c.constraint_id in done]

    def plan_reveal(self, negotiator_utterance: str, action: Action) -> list[BuyerConstraint]:
        """Which constraints to state now (deterministic).

        CLARIFY (D072): ONLY constraints the buyer has ALREADY stated and the negotiator's text touches
        (``_reveal.touched``): the answer restates / firms their hardness or value. It never introduces a new
        constraint, and with nothing touched the answer says that there is nothing further.
        Any other action (a proposal): unrevealed constraints touched by the negotiator's text (D044), capped at
        ``reveal_max_per_answer``. Always empty when progressive reveal is disabled."""
        if not self.reveal_cfg.enabled:
            return []
        if action == Action.CLARIFY:
            plan = [c for c in self.stated() if touched(c, negotiator_utterance)]
        else:
            plan = [c for c in self.unrevealed() if touched(c, negotiator_utterance)]
        return dedupe_areas(plan, self.reveal_cfg.reveal_max_per_answer)      # one phrasing per requirement

    def reveal(self, negotiator_utterance: str, action: Action, transcript: TranscriptView, seed: int,
               lead_text: str = "", turn_idx: int = 0) -> tuple[str, list[str], list[str]]:
        """Answer a negotiator turn with further requirements -> (utterance, newly_revealed_ids, llm_call_ids).

        ``lead_text`` is the buyer's normal reply in the same turn (a proposal answer); the reveal sentence is
        appended and ``reveal_check`` runs once over the joined text. With nothing to reveal and a non-CLARIFY
        action the call is a no-op returning ``(lead_text, [], [])``; for CLARIFY the LLM is still called with an
        empty list so the buyer says that nothing more is needed."""
        plan = self.plan_reveal(negotiator_utterance, action)
        if not plan and action != Action.CLARIFY:
            return lead_text, [], []
        reqs = [self._requirement(c) for c in plan]
        ctx = self.ctx(turn_idx)
        text, ids = guarded_call(self.runners.buyer_reveal, "buyer_reveal", STUB_REVEAL, self._qa_flags,
                                 self.profile, transcript, reqs, negotiator_utterance, seed=seed, ctx=ctx,
                                 persona=self.persona,
                                 already_stated=[a for a in self.already_stated() if a not in {r.text for r in reqs}])
        full = f"{lead_text} {text}".strip() if lead_text else text
        pending = self.unrevealed()
        confirmed: list[str] = []
        if pending:
            confirmed, rids = guarded_call(self.runners.reveal_check, "reveal_check", [], self._qa_flags,
                                           full, pending, seed=seed, ctx=ctx)
            ids = ids + rids
        confirmed = list(confirmed)
        if action == Action.CLARIFY and confirmed:
            self._qa_flags.append("clarify_answer_revealed_new")     # D072: a clarify answer must not introduce constraints
        cap = self.reveal_cfg.reveal_max_per_answer
        if self.reveal_cfg.enabled and len(confirmed) > cap:
            # D065: the cap is enforced in code, not only in the prompt. Plan ids first, then the rest, in order.
            planned = [c.constraint_id for c in plan]
            confirmed = sorted(confirmed, key=lambda i: (i not in planned, planned.index(i) if i in planned else 0))[:cap]
            self._qa_flags.append("reveal_over_cap")
        return full, self._record_reveals(confirmed), ids

    # ------------------------------------------------------------ D055 proof requests
    def _claiming_candidate(self, c: BuyerConstraint, prefer: set[str]) -> Optional[str]:
        """A candidate whose truth cell for ``c`` is PASS / PENDING (the catalogue claims the attribute)."""
        assert self.truth is not None
        ok = sorted(x.product_id for x in self.truth.cells
                    if x.constraint_ref == c.constraint_id and x.scored
                    and x.status in (Compliance.PASS, Compliance.PENDING_VERIFICATION))
        for pool in (prefer, set(self.truth.feasible), None):
            hit = [p for p in ok if pool is None or p in pool]
            if hit:
                return hit[0]
        return None

    def _proof_requests(self, yes_truth: list[BuyerConstraint], gained: set[str], turn_idx: int
                        ) -> list[tuple[ProofRequest, str]]:
        """-> [(ProofRequest, claim label)] drawn from the buyer rng, deterministic order (constraint id)."""
        cfg = self.proof_cfg
        if not cfg.get("enabled", False) or self.truth is None:
            return []
        rate = float(cfg["rate_by_tier"][self.persona.tier.value])
        cap = int(cfg["max_per_turn"])
        by_pid = {c.product_id: c for c in self.truth.candidates}
        out: list[tuple[ProofRequest, str]] = []
        for c in sorted(yes_truth, key=lambda x: x.constraint_id):
            if len(out) >= cap:
                break
            pid = self._claiming_candidate(c, gained)
            if pid is None:
                continue
            if self.rng.random() < rate:
                pr = ProofRequest(turn_idx=turn_idx, product_id=pid, constraint_id=c.constraint_id,
                                  attribute=c.attribute, doc_name=proof_doc_name(c))
                cand = by_pid[pid]
                out.append((pr, f"{cand.supplier_name or cand.product_name} lists it as meeting this requirement"))
        return out

    # ------------------------------------------------------------ proposal response
    def respond_to_proposal(self, bundle: RelaxationBundle, resolved: Mapping[str, Optional[BuyerConstraint]],
                            transcript: TranscriptView, seed: int, turn_idx: int,
                            item_labels: Optional[Mapping[str, str]] = None) -> BuyerResponse:
        """Decide, apply to the truth state, draw proof requests, verbalise. ``resolved``: proposal ref -> the truth
        constraint the belief stands for (None = unmatched). Mutates ``self.truth``."""
        truth = self.truth
        if truth is None:
            raise RuntimeError("BuyerSimulator.respond_to_proposal needs a TruthState")
        item_labels = item_labels or {}
        base = dict(truth.levels)
        bounds = truth.state.bounds
        entries: list[dict[str, Any]] = []
        decide_items: list[ProposalItem] = []
        not_permitted: list[str] = []
        proposal: dict[str, int] = {}
        seen: dict[str, int] = {}
        for opt in bundle.items:
            tc = resolved.get(opt.constraint_ref)
            e: dict[str, Any] = {"opt": opt, "tc": tc, "rule": None, "r": None, "pre": None, "dup_of": None}
            entries.append(e)
            if tc is None:
                self._qa_flags.append(f"unmatched_belief:{opt.constraint_ref}")
                e["pre"] = (Action.YES, "unmatched")
                continue
            if tc.constraint_id in seen:
                e["dup_of"] = seen[tc.constraint_id]
                continue
            seen[tc.constraint_id] = len(entries) - 1
            rule = truth.by_ref.get(tc.constraint_id)
            if rule is None:                                    # not a relaxable rule (D053 gates)
                decide_items.append(ProposalItem(ref=tc.constraint_id, level=1, max_level=1, hardness=tc.hardness,
                                                 category=tc.constraint_category, doc_proof=tc.is_doc_proof))
                if not tc.is_doc_proof:
                    not_permitted.append(tc.constraint_id)
                continue
            e["rule"] = rule
            if rule.mode == "set":                              # D076: supplier-state widening
                r = state_level_from_text(rule, opt.to_value)
            else:
                frac = change_fraction(opt.from_value, opt.to_value)
                r = level_ceil(rule.levels, frac) if frac is not None else None
            e["r"] = r
            cur = base[rule.ref]
            if r is None:
                e["pre"] = (Action.NO, "out_of_range")
            elif r <= cur:
                e["pre"] = (Action.YES, "already")
            elif r not in domain_levels(rule, bounds):
                e["pre"] = (Action.NO, "bounded")
            else:
                proposal[rule.ref] = r
                decide_items.append(ProposalItem(ref=rule.ref, level=r, max_level=rule.top, hardness=rule.hardness,
                                                 category=rule.category, doc_proof=False, current_level=cur))
        accepted_rules = [k for k, b in bounds.items() if "fixed" in b]
        pool = len({c.supplier_id for c in truth.candidates})
        dec = buyer_decide(decide_items, self.params, self.rng, accepted_rules=accepted_rules, pool=pool,
                           not_permitted=not_permitted)
        by_ref_dec = {d.ref: d for d in dec.items}
        res = truth.apply(proposal, [d.outcome() for d in dec.items if d.ref in truth.by_ref or d.action == Action.BYPASS],
                          turn=turn_idx,
                          justification={"turn": turn_idx})
        errors = list(res.errors)
        if errors:
            self._qa_flags.extend(f"apply_decision:{e}" for e in errors)
            raise RuntimeError(f"invalid relaxation transition: {errors}")

        responses: list[ItemResponse] = []
        record: dict[str, dict] = {}
        yes_truth: list[BuyerConstraint] = []
        gained: set[str] = set()
        accepted: list[RelaxationOption] = []
        running = dict(base)
        for i, e in enumerate(entries):
            opt, tc, rule = e["opt"], e["tc"], e["rule"]
            action, cv, reason, level = Action.NO, None, "", e["r"]
            if e["dup_of"] is not None:
                first = responses[e["dup_of"]]
                action, cv, reason = first.action, first.counter_value, "duplicate"
            elif e["pre"] is not None:
                action, reason = e["pre"]
            else:
                d = by_ref_dec[tc.constraint_id if rule is None else rule.ref]
                action, reason, level = d.action, d.reason, d.level
                if action == Action.COUNTER and rule is not None and rule.mode == "set":
                    cv = level_target(rule, d.level)
                elif action == Action.COUNTER and rule is not None:
                    ratio = float(rule.levels[d.level]) / float(rule.levels[e["r"]])
                    cv = scale_value(opt.from_value, opt.to_value, ratio) or level_target(rule, d.level)
            # D059: constraint_ref = TRUTH id; an unmatched belief gets `unmatched:<belief_id>` (never a bare
            # belief id, flagged `unmatched_belief:*`); belief_ref = the negotiator's id for the same proposal item.
            responses.append(ItemResponse(constraint_ref=(tc.constraint_id if tc is not None
                                                          else f"unmatched:{opt.constraint_ref}"),
                                          belief_ref=opt.constraint_ref, action=action, counter_value=cv))
            record[opt.constraint_ref] = {"constraint_id": tc.constraint_id if tc is not None else None,
                                          "action": action.value, "proposed_level": e["r"], "level": level,
                                          "reason": reason, "counter_value": cv}
            if action == Action.YES:
                tg = 0
                if rule is not None and e["pre"] is None and e["dup_of"] is None:
                    nxt = dict(running)
                    nxt[rule.ref] = e["r"]
                    g = role_aware_gain(truth.originals, truth.candidates, truth.ctx, rules=truth.rules,
                                        levels_before=running, levels_after=nxt)
                    running, tg = nxt, g.gain_products
                    gained |= set(g.gained)
                    yes_truth.append(tc)
                accepted.append(opt.model_copy(update={"truth_gain": tg}))

        actions = {r.action for r in responses}
        if len(actions) == 1:
            turn_action = next(iter(actions))
            outcome = _OUTCOME[turn_action]
        else:
            turn_action, outcome = Action.MIXED, "PARTIAL"
        counters = [r for r in responses if r.action == Action.COUNTER]
        counter_value = counters[0].counter_value if len(counters) == 1 else None

        proofs = self._proof_requests(yes_truth, gained, turn_idx)
        self.proof_log.extend(p for p, _ in proofs)

        items: list[BuyerItemDecision] = []
        for e, r in zip(entries, responses):
            tc = e["tc"]
            label = self._label(tc) if tc is not None else item_labels.get(e["opt"].constraint_ref,
                                                                           e["opt"].constraint_ref)
            items.append(BuyerItemDecision(
                item=label, action=r.action.value,
                strength="must" if (tc is not None and tc.hardness == Hardness.HARD) else "prefer",
                proposed_value=self._spoken(e["rule"], e["r"], e["opt"].to_value),
                counter_value=(self._spoken(e["rule"], record[e["opt"].constraint_ref]["level"], r.counter_value)
                               if r.counter_value is not None else None),
                doc_name=((tc.doc_name or tc.attribute) if (r.action == Action.BYPASS and tc is not None) else None)))
        by_cid = {c.constraint_id: c for c in truth.originals}
        for pr, claim in proofs:
            tc = by_cid[pr.constraint_id]
            items.append(BuyerItemDecision(item=self._label(tc), action="PROOF_REQUEST",
                                           strength="must" if tc.hardness == Hardness.HARD else "prefer",
                                           doc_name=pr.doc_name, claimed=claim))
            responses.append(ItemResponse(constraint_ref=pr.constraint_id, action=Action.BYPASS))
        stub = STUB_REPLY + "".join(f" Please send the {p.doc_name}." for p, _ in proofs)
        utt, ids = guarded_call(self.runners.buyer_reply, "buyer_reply", stub, self._qa_flags,
                                self.profile, transcript, turn_action.value, items, seed=seed,
                                ctx=self.ctx(turn_idx), persona=self.persona, already_stated=self.already_stated())
        return BuyerResponse(action=turn_action, outcome=outcome, counter_value=counter_value, responses=responses,
                             proof_requests=[p for p, _ in proofs], accepted=accepted, decision=record,
                             utterance=utt, llm_call_ids=ids,
                             # D100: any cooperative/progressive action (YES / COUNTER / BYPASS) counts as progress
                             yes=bool(actions & {Action.YES, Action.COUNTER, Action.BYPASS}))
