"""Joined document feedback uses the named plan wake kind."""
from plans import plan_wake


def test_the_wake_kind_is_a_named_plan_wake():
    assert plan_wake.INTENT_INPUT == "intent_input"
