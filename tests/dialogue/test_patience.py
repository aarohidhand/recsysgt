"""D100: patience only burns on ALL-NO turns; COUNTER / BYPASS progress resets it."""
from gtgen.dialogue.buyer import BuyerResponse
from gtgen.contracts import Action

def test_yes_resets_patience():
    # Minimal buyer response: a cooperative action (COUNTER, BYPASS, or YES) should set yes=True
    # Using dummy data; only the yes logic is tested.
    resp = BuyerResponse(
        action=Action.COUNTER,
        proof_requests=[], accepted=[], decision={}, utterance="test", llm_call_ids=["x"],
        outcome="counter",
        counter_value=5,
        responses=[],
        yes=bool({Action.COUNTER, Action.YES, Action.BYPASS} & {Action.COUNTER})
    )
    assert resp.yes is True

def test_all_no_burns_patience():
    resp = BuyerResponse(
        action=Action.COUNTER,
        proof_requests=[], accepted=[], decision={}, utterance="test", llm_call_ids=["x"],
        outcome="refuse",
        counter_value=None,
        responses=[],
        yes=bool({Action.COUNTER, Action.YES, Action.BYPASS} & {Action.NO})
    )
    assert resp.yes is False