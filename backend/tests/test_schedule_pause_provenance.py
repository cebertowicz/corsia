"""The pause contract's canonical human-input classification."""

from __future__ import annotations

import json

import pytest

from plans.pause_authority import is_users_word


@pytest.mark.parametrize(
    ("role", "metadata", "expected"),
    [
        ("user", {}, True),
        ("user", {"sender_kind": "agent"}, False),
        ("user", {"sender_kind": "eval"}, False),
        ("user", {"completion": True}, False),
        ("user", {"routed_event": "x"}, False),
        ("user", {"completion": False}, True),
        ("user", {"sender_kind": "system"}, False),
        ("system", {"event_type": "plan.decision_answered"}, True),
        ("system", {"event_type": "automation.scheduled_wake"}, False),
    ],
)
def test_is_users_word_uses_the_canonical_non_human_markers(role, metadata, expected):
    assert is_users_word({"role": role, "metadata": json.dumps(metadata)}) is expected
