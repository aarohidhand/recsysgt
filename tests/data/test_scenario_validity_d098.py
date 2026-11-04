"""D098: scenario_validity behavior after loosest/no_soft_path demotion."""
from gtgen.data.calibrate import scenario_validity

def test_no_negotiated_constraint_still_skips():
    v = scenario_validity(0, 0, 0, 0, 3)
    assert v["valid"] is False
    assert "no_negotiated_product_constraint" in v["reasons"]
    assert "loosest_below_min_shortlist" not in v["reasons"]
    assert "no_soft_relaxation_path" not in v["reasons"]
    assert v["checks"]["loosest_below_min_shortlist"] is True
    assert v["checks"]["no_soft_relaxation_path"] is True

def test_loosest_below_now_informational_only():
    # n_negotiated >= 1, loose < min, but no soft path -> valid=True (D098)
    v = scenario_validity(2, 0, 0, 0, 3)
    assert v["valid"] is True
    assert v["reasons"] == []
    assert v["checks"]["loosest_below_min_shortlist"] is True
    assert v["checks"]["no_soft_relaxation_path"] is True
