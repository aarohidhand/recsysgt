"""Small synthetic fixtures for dialogue tests: 3 constraints, 3 candidates, fake LLM runners."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable, Optional

import pytest
import yaml

from gtgen.contracts import (
    AttributeAlignment, BelievedConstraint, BuyerConstraint, BuyerProfile, CandidateConstraint,
    CandidateView, CatalogueView, Hardness, Persona, ScenarioBrief, Tier,
)
from gtgen.llm import InterpretItem, LLMResult

ROOT = Path(__file__).resolve().parents[2]
BID = "GEM/2019/B/242710"


def _load_settings() -> dict:
    with open(ROOT / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)


def legacy_levels(st: dict) -> dict:
    """Mechanics tests were hand-computed on a 0 / 15 % / 30 % level grid; pin it here so they do not move when the shipped
    ``relaxation.levels`` change.  Shipped-default behaviour is pinned by ``test_golden_default_settings.py`` only."""
    st["relaxation"] = {**st["relaxation"], "level_mode": "relative", "default_max_deviation": 0.3,
                        "levels": [0.0, 0.15, 0.30]}
    return st


@pytest.fixture(scope="session")
def settings() -> dict:
    """Settings for the pre-D044 flow tests: progressive reveal OFF (the opening states everything, no CLARIFY).
    Tests of the progressive reveal use ``pr_settings``."""
    st = legacy_levels(_load_settings())
    st["dialogue"]["progressive_reveal"]["enabled"] = False
    return st


@pytest.fixture
def pr_settings() -> dict:
    """config/settings.yaml as shipped (progressive reveal ON)."""
    st = legacy_levels(_load_settings())
    assert st["dialogue"]["progressive_reveal"]["enabled"] is True
    return st


def bc(cid: str, attr: str, op: Optional[str], value: Optional[str], *, hard: bool = False,
       unit: Optional[str] = None, doc: bool = False, doc_name: Optional[str] = None,
       category: Optional[str] = None, text: Optional[str] = None) -> BuyerConstraint:
    return BuyerConstraint(constraint_id=cid, bid_id=BID, golden_param=hard,
                           hardness=Hardness.HARD if hard else Hardness.SOFT, attribute=attr,
                           operator=op, buyer_value=value, unit=unit, is_doc_proof=doc, doc_name=doc_name,
                           constraint_category=category, commercial_text=text)


def cc(pid: str, attr: str, value: str, unit: Optional[str] = None) -> CandidateConstraint:
    return CandidateConstraint(product_id=pid, supplier_id="s_" + pid, constraint_id=f"{pid}_{attr}",
                               attribute=attr, value=value, unit=unit, raw_text_verified=True, confidence=1.0)


def belief(bid: str, attr: str, op: str, value: str, *, hard: bool = False, unit: Optional[str] = None,
           doc: bool = False) -> BelievedConstraint:
    return BelievedConstraint(belief_id=bid, attribute=attr, operator=op, value=value, unit=unit,
                              believed_hardness=Hardness.HARD if hard else Hardness.SOFT, is_doc_proof=doc,
                              source_turn=0)


@pytest.fixture
def constraints() -> list[BuyerConstraint]:
    return [
        bc("C1", "motor_rating", ">=", "1.5", hard=True, unit="HP", category="TECHNICAL",
           text="Motor rating of at least 1.5 HP"),
        bc("C2", "delivery_days", "<=", "15", unit="days", category="DELIVERY",
           text="Delivery within 15 days"),
        bc("C3", "iso_certification", "=", "ISO 9001", doc=True, doc_name="ISO 9001 certificate",
           category="QUALITY", text="ISO 9001 certified manufacturer"),
    ]


@pytest.fixture
def brief(constraints) -> ScenarioBrief:
    return ScenarioBrief(scenario_id="syn_s1", bid_ref=BID, quantity=3, quantity_unit="units",
                         budget_inr=60000.0, constraints=constraints)


@pytest.fixture
def catalogue() -> CatalogueView:
    def cand(pid: str, days: str, hp: str, price: float, extra_specs: Optional[dict] = None) -> CandidateView:
        specs = {"Power": f"{hp} HP", "Delivery": f"{days} days"}
        specs.update(extra_specs or {})
        return CandidateView(product_id=pid, supplier_id="s_" + pid, product_name=f"Pump {pid}",
                             category_name="Submersible Pumps", price=price, price_formatted=f"Rs {price:,.0f}",
                             supplier_name=f"Supplier {pid}", city="Pune", rating=4.0, rating_count=10,
                             member_since="5 yrs", specifications=specs,
                             constraints=[cc(pid, "motor_rating", hp, "HP"), cc(pid, "delivery_days", days, "days")])
    return CatalogueView(scenario_id="syn_s1", candidates=[
        cand("P1", "10", "2", 15000), cand("P2", "17", "2", 14000),
        cand("P3", "19", "2", 13000, {"Note": "relevance_score 0.9"}),
    ])


@pytest.fixture
def alignments() -> list[AttributeAlignment]:
    return [AttributeAlignment(bid_id=BID, buyer_attribute=a, candidate_attribute=a, confidence=1.0,
                               llm_call_id="align_t") for a in ("motor_rating", "delivery_days")]


@pytest.fixture
def profile() -> BuyerProfile:
    return BuyerProfile(company_name="Kaveri Agro", industry="Irrigation", city="Pune",
                        style="Brief.", llm_call_id="prof_t")


def make_persona(tau: float, tier: Tier, *, counter_band: float = 0.3, cap: int = 2, patience: int = 4,
                 tolerance: Optional[dict] = None, max_relaxations: int = 3, patience_rounds: int = 3,
                 decision_randomness: float = 0.1, level_tolerance: Optional[dict] = None, seed: int = 1) -> Persona:
    """D053 persona: ``level_tolerance`` = T_c per constraint category in LEVELS (default 2 = the top level for every
    category); ``tolerance`` is the legacy category multiplier dict."""
    return Persona(persona_seed=seed, tau=tau, tau_band=None, tier=tier,
                   category_tolerance=tolerance or {"TECHNICAL": 1.0, "DELIVERY": 1.0, "QUALITY": 1.0},
                   counter_band=counter_band, patience_turns=patience, max_concessions_per_turn=cap,
                   ranking_weights={"spec_match": 0.5, "price_score": 0.5}, llm_call_id="pers_t",
                   max_relaxations=max_relaxations, patience_rounds=patience_rounds,
                   decision_randomness=decision_randomness,
                   tolerance=level_tolerance if level_tolerance is not None
                   else {"TECHNICAL": 2.0, "DELIVERY": 2.0, "QUALITY": 2.0, "GENERAL": 2.0})


@pytest.fixture
def lenient_persona() -> Persona:
    return make_persona(0.1, Tier.LENIENT)


@pytest.fixture
def strict_persona() -> Persona:
    return make_persona(0.8, Tier.STRICT)


DEFAULT_BELIEFS = [
    belief("B1", "motor_rating", ">=", "1.5", hard=True, unit="HP"),
    belief("B2", "delivery_days", "<=", "15", unit="days"),
]


class FakeRunners:
    """Duck-typed stand-in for LLMRunners. Records every call's arguments."""

    def __init__(self, beliefs: Optional[list[BelievedConstraint]] = None,
                 beliefs_fn: Optional[Callable[[Any], list[BelievedConstraint]]] = None,
                 interpret: Any = None) -> None:
        """``interpret``: None (all UNCLEAR), a list of per-call scripts (each a list of ``(response,
        counter_value)`` per item index; the last script repeats), a callable ``(transcript, items) -> script``
        or the string ``"fail"`` (returns value None)."""
        self.interpret = interpret
        self._interp_calls = 0
        self.reveal_fn: Optional[Callable[[str, list], list]] = None
        self.reveal_check_texts = False
        self.clarify_text = "Is there anything else that matters to you?"
        self.beliefs = copy.deepcopy(beliefs if beliefs is not None else DEFAULT_BELIEFS)
        self.beliefs_fn = beliefs_fn
        self.calls: list[tuple[str, tuple, dict]] = []

    def _rec(self, name: str, args: tuple, kwargs: dict) -> None:
        self.calls.append((name, args, kwargs))

    def buyer_opening(self, *a, **k):
        self._rec("buyer_opening", a, k)
        return LLMResult("Hello, we need 3 units of pumps, budget Rs 60,000. Motor at least 1.5 HP.", ["id_open"])

    def reveal_check(self, utterance, constraints, seed=None, ctx=None):
        """Default: C1 is stated by the opening; later calls confirm any constraint whose commercial_text
        appears verbatim in the utterance (``reveal_fn`` overrides)."""
        self._rec("reveal_check", (utterance, constraints), {"seed": seed})
        if self.reveal_fn is not None:
            return LLMResult(self.reveal_fn(utterance, constraints), ["id_reveal"])
        if not self.reveal_check_texts:
            return LLMResult(["C1"], ["id_reveal"])
        return LLMResult([c.constraint_id for c in constraints if (c.commercial_text or "\0") in utterance],
                         ["id_reveal"])

    def buyer_reply(self, *a, **k):
        self._rec("buyer_reply", a, k)
        return LLMResult("Understood, here is my answer.", ["id_breply"])

    def negotiator_extract(self, *a, **k):
        self._rec("negotiator_extract", a, k)
        bs = self.beliefs_fn(a[0]) if self.beliefs_fn else self.beliefs
        return LLMResult(copy.deepcopy(bs), ["id_extract"])

    def negotiator_interpret(self, transcript, proposal_items, seed=None, ctx=None):
        self._rec("negotiator_interpret", (transcript, proposal_items), {"seed": seed})
        n = self._interp_calls
        self._interp_calls += 1
        if self.interpret == "fail":
            return LLMResult(None, ["id_interp"], ["negotiator_interpret: failed"])
        if callable(self.interpret):
            script = self.interpret(transcript, proposal_items)
        elif isinstance(self.interpret, list):
            script = self.interpret[min(n, len(self.interpret) - 1)]
        else:
            script = [("UNCLEAR", None)] * len(proposal_items)
        items = [InterpretItem(index=it["index"], response=r, counter_value=cv)
                 for it, (r, cv) in zip(proposal_items, script)]
        return LLMResult(items, ["id_interp"])

    def negotiator_reply(self, *a, **k):
        self._rec("negotiator_reply", a, k)
        return LLMResult(("Could you accept a slightly longer delivery time?", []), ["id_nreply"])

    # ---- D054 LLM-worded final turns (test double with the runner's exact signature)
    def negotiator_final(self, kind, facts, sales_persona=None, seed=None, ctx=None):
        self._rec("negotiator_final", (kind, facts), {"sales_persona": sales_persona, "seed": seed})
        names = "; ".join(f"{o['product']} ({o['supplier']})" for o in facts["options"]) or "nothing"
        return LLMResult(f"[{kind}] Options: {names}.", ["id_final"])

    # ---- D044 progressive reveal
    def negotiator_clarify(self, transcript, topics, known, seed=None, **kw):
        self._rec("negotiator_clarify", (transcript, topics, known), {"seed": seed, **kw})
        return LLMResult((self.clarify_text, []), ["id_clarify"])

    def buyer_reveal(self, profile, transcript, requirements, last_question, seed=None, **kw):
        self._rec("buyer_reveal", (profile, transcript, requirements, last_question), {"seed": seed, **kw})
        if not requirements:
            return LLMResult("Nothing more from our side.", ["id_reveal_say"])
        return LLMResult("Also: " + "; ".join(r.text for r in requirements) + ".", ["id_reveal_say"])


def final_calls(runners) -> list[tuple[str, dict]]:
    """(kind, facts) of every ``negotiator_final`` call, for FakeRunners and the golden Runners alike."""
    return [(a[0], a[1]) for n, a, _ in runners.calls if n == "negotiator_final"]


@pytest.fixture
def fake_runners() -> FakeRunners:
    return FakeRunners()


# ---------------------------------------------------------------- D048-D052 scenario helpers
from gtgen.contracts import DocClaim, ScenarioRole  # noqa: E402
from gtgen.llm import DocClaimRequest, listing_text  # noqa: E402


def claim_for(c: CandidateView, doc: str, ref: str, *, max_chars: int = 1500) -> DocClaim:
    """Deterministic stand-in for the quote-verified claim extraction: claims iff ``doc`` occurs in the listing."""
    text = listing_text(c, max_chars)
    hit = doc.lower() in text.lower()
    if hit:
        i = text.lower().index(doc.lower())
        return DocClaim(product_id=c.product_id, constraint_ref=ref, claims=True, quote=text[i:i + len(doc)],
                        quote_verified=True, llm_call_id="claim_t")
    return DocClaim(product_id=c.product_id, constraint_ref=ref, claims=False, llm_call_id="claim_t")


class ClaimRunners(FakeRunners):
    """FakeRunners plus ``doc_claims`` (the plain FakeRunners has none, which keeps the legacy path)."""

    def __init__(self, *a, doc_mode: str = "ok", cands: Optional[list[CandidateView]] = None, **k) -> None:
        super().__init__(*a, **k)
        self.doc_mode = doc_mode            # ok | fail | missing | stub_failed
        self.cands = {c.product_id: c for c in (cands or [])}
        self.doc_requests: list[list[DocClaimRequest]] = []

    def doc_claims(self, requests, seed=None, ctx=None):
        self._rec("doc_claims", (requests,), {"seed": seed})
        self.doc_requests.append(list(requests))
        if self.doc_mode == "missing":
            raise FileNotFoundError("Mock fixture not found")
        if self.doc_mode == "fail":
            return LLMResult(None, ["id_claim"], ["doc_claim: failed"])
        if self.doc_mode == "stub_failed":
            return LLMResult([DocClaim(product_id=r.product_id, constraint_ref=r.constraint_ref, claims=False,
                                       llm_call_id="failed") for r in requests], ["id_claim"],
                             ["doc_claim batch0: failed"])
        out = []
        for r in requests:
            out.append(DocClaim(product_id=r.product_id, constraint_ref=r.constraint_ref,
                                claims=r.doc_name.lower() in r.listing_text.lower(),
                                quote=(r.doc_name if r.doc_name.lower() in r.listing_text.lower() else None),
                                quote_verified=r.doc_name.lower() in r.listing_text.lower(),
                                llm_call_id="claim_t"))
        return LLMResult(out, ["id_claim"])


# ---------------------------------------------------------------- D053/D055/D056 echo runners
import re as _re  # noqa: E402


class EchoRunners(FakeRunners):
    """A matched pair of fake verbaliser / interpreter that keeps the barrier honest: the buyer reply SAYS each
    decision in text, the negotiator interpreter READS the last buyer message (never the hidden decision).

    buyer text:  "<item>: yes, <value> works."  |  "<item>: no."  |  "<item>: counter at <v>."  |
                 "<item>: ok, will send the <doc>."  |  "Please send the <doc>." (proof request)
    """

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.reveal_check_texts = True
        self.ack_calls: list[tuple] = []

    def buyer_opening(self, profile, category, quantity, unit, budget, reqs, seed=None, **kw):
        self._rec("buyer_opening", (profile, category, quantity, unit, budget, reqs), {"seed": seed, **kw})
        return LLMResult(f"Hello, we need {quantity:g} {unit} of {category}, budget Rs {budget:,.0f}. "
                         + "; ".join(r.text for r in reqs) + ".", ["id_open"])

    def buyer_reply(self, profile, transcript, turn_action, items, seed=None, **kw):
        self._rec("buyer_reply", (profile, transcript, turn_action, items), {"seed": seed, **kw})
        parts = []
        for it in items:
            if it.action == "YES":
                parts.append(f"{it.item}: yes, {it.proposed_value} works.")
            elif it.action == "COUNTER":
                parts.append(f"{it.item}: counter at {it.counter_value}.")
            elif it.action == "BYPASS":
                parts.append(f"{it.item}: ok, will send the {it.doc_name}.")
            elif it.action == "PROOF_REQUEST":
                parts.append(f"Please send the {it.doc_name}.")
            else:
                parts.append(f"{it.item}: no.")
        return LLMResult(" ".join(parts), ["id_breply"])

    def negotiator_interpret(self, transcript, proposal_items, seed=None, ctx=None):
        self._rec("negotiator_interpret", (transcript, proposal_items), {"seed": seed, "ctx": ctx})
        text = next((t.utterance for t in reversed(transcript.turns) if t.speaker.value == "BUYER"), "")
        segs = _re.findall(r"([^.:]+?): (yes|no|counter at ([\d.]+\d))", text)          # (label, verdict, counter value)
        items = []
        for it in proposal_items:
            want = set(_re.findall(r"[a-z0-9]+", it["attribute"].lower()))
            best = max(segs, key=lambda sg: len(want & set(_re.findall(r"[a-z0-9]+", sg[0].lower()))), default=None)
            if best is None or not (want & set(_re.findall(r"[a-z0-9]+", best[0].lower()))):
                r, cv = "UNCLEAR", None
            elif best[1] == "yes":
                r, cv = "ACCEPT", None
            elif best[1] == "no":
                r, cv = "REFUSE", None
            else:
                r, cv = "COUNTER", best[2]
            items.append(InterpretItem(index=it["index"], response=r, counter_value=cv))
        return LLMResult(items, ["id_interp"])

    def negotiator_reply(self, transcript, bundle, beliefs, evidence, seed=None, **kw):
        self._rec("negotiator_reply", (transcript, bundle, beliefs, evidence), {"seed": seed, **kw})
        mp = kw["menu_point"]
        return LLMResult(("Could you consider " + "; ".join(f"{i.attribute} at {i.to_value}" for i in mp.items)
                          + "?", []), ["id_nreply"])

    def negotiator_acknowledge(self, transcript, proof_requests, evidence, seed=None, **kw):
        self.ack_calls.append((proof_requests, evidence))
        self._rec("negotiator_acknowledge", (transcript, proof_requests, evidence), {"seed": seed, **kw})
        return LLMResult(("Noted, the supplier will provide the "
                          + " and the ".join(p.doc_name for p in proof_requests) + ".",
                          [p.evidence_id for p in proof_requests]), ["id_ack"])


class ScriptedRng:
    """Stand-in for the buyer's ``random.Random``: ``random()`` returns the scripted values, then the last one forever."""

    def __init__(self, values) -> None:
        self.values = list(values)
        self.n = 0

    def random(self) -> float:
        v = self.values[min(self.n, len(self.values) - 1)]
        self.n += 1
        return v
