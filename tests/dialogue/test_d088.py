"""D088: robust alignment/normalisation of common cable extractor wording.

The 209245 extractor emitted ``cable_size`` (which used to tie with ``cable_type``), ``insulation``/``sheath``
(verbose values) and ``number_of_cores='3'`` against catalogue ``'3 Core'``.  Those cells were UNKNOWN, so the
verified-feasible gate produced ``BEST_AVAILABLE``/``nothing_verified`` after two turns.  These tests pin the
deterministic fixes.
"""
from __future__ import annotations

from gtgen.contracts import (
    Action, AttributeAlignment, CandidateView, CatalogueView, Compliance, Speaker, TranscriptTurn, TranscriptView,
)
from gtgen.dialogue import Negotiator
from gtgen.dialogue._util import alignment_dict, pair_score, per_belief_alignments
from gtgen.matching.evidence import as_view, evaluate, extract_evidence
from gtgen.matching.units import convert

from dialogue.helpers import FakeRunners, belief, cc


def _al(buyer: str, candidate: str) -> AttributeAlignment:
    return AttributeAlignment(bid_id="", buyer_attribute=buyer, candidate_attribute=candidate,
                              confidence=1.0, llm_call_id="d088")


def test_cable_aliases_beat_plain_token_overlap():
    assert pair_score("cable_size", "size_sqmm") == 3.0
    assert pair_score("cable_size", "cable_type") < 3.0
    assert pair_score("cable_size_min", "size_sqmm") == 3.0
    assert pair_score("cable_size_max", "size_sqmm") == 3.0
    assert pair_score("insulation", "insulation_type") == 3.0
    assert pair_score("sheath", "sheath_type") == 3.0
    assert pair_score("submersible_cable_quantity", "total_quantity") == 3.0
    assert pair_score("standard_cable_length_in_a_Coil/Reel", "standard_cable_length") == 3.0
    assert pair_score("name_of_the_lab", "name_of_lab") == 3.0
    assert pair_score("technical_specifications", "applicable_standard") == 3.0


def test_cable_size_range_beliefs_share_one_size_alignment_and_pass():
    adict = alignment_dict([_al("size_sqmm", "size")])
    beliefs = [belief("B1", "cable_size_min", ">=", "2.5", hard=True, unit="Sqmm"),
               belief("B2", "cable_size_max", "<=", "16", hard=True, unit="Sqmm")]
    pba = per_belief_alignments(beliefs, adict)
    assert pba["cable_size_min"].buyer_attribute == "size_sqmm"
    assert pba["cable_size_max"].buyer_attribute == "size_sqmm"
    cand = CandidateView(product_id="P1", supplier_id="S1", product_name="Radix cable",
                         constraints=[cc("P1", "size", "4", "Sq mm")])
    for b in beliefs:
        ev = extract_evidence(as_view(b), cand, pba[b.attribute])
        assert evaluate(as_view(b), ev) == Compliance.PASS


def test_cable_size_aligns_to_size_sqmm_not_cable_type():
    adict = alignment_dict([_al("size_sqmm", "size"), _al("cable_type", "cable_type")])
    pba = per_belief_alignments([belief("B1", "cable_size", "in", "2.5, 4, 6, 10, 16",
                                        hard=True, unit="Sqmm")], adict)
    assert pba["cable_size"].buyer_attribute == "size_sqmm"


def test_core_units_convert():
    assert convert(3, "Core", "cores") == 3.0
    assert convert(3, "cores", "Core") == 3.0


def test_number_of_cores_passes_against_core_suffix():
    b = belief("B1", "number_of_cores", "=", "3", hard=True, unit="cores")
    cand = CandidateView(product_id="P1", supplier_id="S1", product_name="Cable",
                         constraints=[cc("P1", "number_of_cores", "3 Core")])
    ev = extract_evidence(as_view(b), cand, _al("number_of_cores", "number_of_cores"))
    assert evaluate(as_view(b), ev) == Compliance.PASS


def _cable_catalogue() -> CatalogueView:
    def cand(pid: str) -> CandidateView:
        return CandidateView(
            product_id=pid, supplier_id="s_" + pid, product_name=f"Radix cable {pid}",
            price=40.0, moq=1000.0,
            specifications={"Size": "4", "No of Cores": "3 Core"},
            constraints=[cc(pid, "size", "4", "Sq mm"), cc(pid, "number_of_cores", "3 Core"),
                         cc(pid, "conductor_material", "Copper (EC grade)"),
                         cc(pid, "insulation_type", "HR"), cc(pid, "sheath_type", "ST2"),
                         cc(pid, "cable_type", "Flat")],
        )
    return CatalogueView(scenario_id="GEM_2019_B_209245_s1", candidates=[cand(f"P{i}") for i in range(1, 6)])


def _209245_beliefs():
    return [
        belief("B1", "submersible_cable_quantity", "=", "5000", hard=True, unit="units"),
        belief("B2", "conductor_material", "=", "Copper (EC grade) stranded conductor", hard=True),
        belief("B3", "number_of_cores", "=", "3", hard=True, unit="cores"),
        belief("B4", "cable_size", "in", "2.5, 4, 6, 10, 16", hard=True, unit="Sqmm"),
        belief("B5", "insulation", "=", "Heat-resistant cables (HR) as per IS 5831 latest", hard=True),
        belief("B6", "sheath", "=", "Type ST2 sheath with 85°C HRPVC compound to IS 5831 latest", hard=True),
        belief("B7", "budget_inr", "<=", "345942", hard=True, unit="INR"),
        belief("B8", "supplier_state", "in", "Gujarat"),
    ]


def test_209245_shape_is_verified_after_normalisation(settings):
    cat = _cable_catalogue()
    alignments = [_al("size_sqmm", "size"), _al("number_of_cores", "number_of_cores"),
                  _al("conductor_material", "conductor_material"),
                  _al("insulation_type", "insulation_type"), _al("sheath_type", "sheath_type"),
                  _al("cable_type", "cable_type")]
    neg = Negotiator(cat, FakeRunners(beliefs=_209245_beliefs()), settings,
                     alignments=alignments, verification_roles=True)
    transcript = TranscriptView(scenario_id="GEM_2019_B_209245_s1", turns=[
        TranscriptTurn(turn_idx=0, speaker=Speaker.BUYER,
                       utterance="We need 5000 units of submersible cable, 3 cores, size 4 Sqmm."),
    ])
    step = neg.step(transcript, cat)
    assert step.action != Action.BEST_AVAILABLE
    assert neg.last_analysis["verified_feasible"] > 0
    assert "nothing_verified" not in neg.drain_qa_flags()
    values = {b.attribute: b.value for b in step.beliefs}
    assert values["conductor_material"] == "Copper (EC grade)"
    assert values["insulation"] == "HR"
    assert values["sheath"] == "ST2"
    assert values["cable_size"] == "2.5, 4, 6, 10, 16"
