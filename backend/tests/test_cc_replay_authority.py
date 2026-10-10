from __future__ import annotations

from types import SimpleNamespace

import pytest


def _prepared_destination(*, is_subagent: bool):
    from subagents.cc_context_types import CCPreparedDestination

    destination = CCPreparedDestination(
        thread_id="thread-1",
        current_user_text="current",
        target_catalog_id="claude-code/opus",
        mode="exact_replay",
        is_subagent=is_subagent,
        working_dir="/workspace",
        cc_session_id="provider-session",
        prior_cc_session_id=None,
        live_handle_fingerprint=None,
        seed_reconstruction=object(),
        frame_reconstruction=object(),
        transcript=object(),
        history=(),
        checkpoint_text=None,
        tool_registry=None,
        tool_definitions=(),
        input_tokens=1,
        source_tokens=0,
        source_section_tokens={},
        fit_section_tokens={},
    )
    tail = SimpleNamespace(
        thread_id="thread-1",
        history_epoch=7,
        sealed_boundary="boundary-7",
    )
    replay = SimpleNamespace(
        mode="exact",
        tail=tail,
        history_epoch=7,
        sealed_boundary="boundary-7",
    )
    prepared = SimpleNamespace(
        target_catalog_id="claude-code/opus",
        mode="exact_replay",
        history_epoch=7,
        sealed_boundary="boundary-7",
        replay=replay,
        rendered=SimpleNamespace(payload=destination),
    )
    return prepared


@pytest.mark.parametrize("is_subagent", [False, True])
def test_prepared_cold_replay_authority_never_branches_on_thread_kind(
    is_subagent,
) -> None:
    from subagents.cc_replay_authority import require_cc_replay_authority

    authority = require_cc_replay_authority(
        "thread-1",
        "current",
        target_catalog_id="claude-code/opus",
        prepared_context=_prepared_destination(is_subagent=is_subagent),
    )

    assert authority.source == "exact_replay"
    assert authority.replay.tail.thread_id == "thread-1"


def test_replay_authority_rejects_missing_prepared_context() -> None:
    from subagents.cc_replay_authority import require_cc_replay_authority

    with pytest.raises(ValueError, match="prepared context"):
        require_cc_replay_authority(
            "thread-1",
            "current",
            target_catalog_id="claude-code/opus",
            prepared_context=None,
        )
