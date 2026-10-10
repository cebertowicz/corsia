"""Thread identity contract for deferred send-reference effects."""

from __future__ import annotations

import pytest

from infra.send_effects import validate_accepted_effects
from infra.send_types import SendRejected


def test_send_reference_effect_accepts_only_sender_thread_identity():
    effects = {
        "send_ref": {
            "sender_thread_id": "sender-thread",
            "target_title": "Target",
            "target_kind": "subagent",
        }
    }

    assert validate_accepted_effects(effects) == effects


def test_send_reference_effect_has_no_legacy_identity_alias():
    legacy_key = "sender_" + "session_id"
    effects = {
        "send_ref": {
            legacy_key: "sender-thread",
            "target_title": "Target",
            "target_kind": "subagent",
        }
    }

    with pytest.raises(SendRejected) as exc_info:
        validate_accepted_effects(effects)

    assert exc_info.value.detail == (
        f"unknown send_ref fields: ['{legacy_key}']; valid fields are "
        "sender_thread_id, target_kind, target_title"
    )
