"""D099: reveal_check drops hallucinated id instead of aborting scenario."""
from unittest.mock import MagicMock
from gtgen.llm.runners import LLMClient

def test_reveal_check_drops_hallucinated_id_without_skip():
    # Mock client always returns RevealOutput with one bad id
    mock_client = MagicMock()
    mock_client.call = MagicMock(return_value=(MagicMock(revealed_ids=["bad_id_1", "good_id_2"]), "cid_1"))
    mock_client.strict = False
    runner = LLMClient(mock_client, temperature=0.0, max_retries=2)
    # Need a minimal constraint list; use dummy objects with constraint_id
    from gtgen.contracts import BuyerConstraint
    c1 = BuyerConstraint(constraint_id="good_id_2", attribute="test", hardness=2)
    result = runner.reveal_check("hello", [c1], seed=1, ctx=None)
    assert result.value == ["good_id_2"]
    assert any("reveal_check_hallucinated_id_dropped:bad_id_1" in f for f in result.flags)
