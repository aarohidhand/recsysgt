"""D112 tests: categories outside the five core ones get 3 personas. No network."""
from gtgen.llm.strata import personas_for_category


def test_core_categories_keep_their_counts():
    assert personas_for_category("Blanket for CAPFs") == 1
    assert personas_for_category("Submersible Pump Set Three Phase") == 1
    assert personas_for_category("Sluice Valve / NRV") == 2
    assert personas_for_category("Distribution Transformer, 3 Phase") == 2
    assert personas_for_category("Office Bags and Laptop Bags") == 3


def test_other_categories_get_three_personas():
    assert personas_for_category("Safety Footwear as per IS 15298 (Part 2)") == 3
    assert personas_for_category("DI Fittings") == 3
    assert personas_for_category(None) == 3


def test_pump_is_checked_before_cable():
    # Known limit (D112): a cable category that also says "pump" gets the pump count.
    assert personas_for_category("Submersible Cable for Submersible Pumps") == 1
