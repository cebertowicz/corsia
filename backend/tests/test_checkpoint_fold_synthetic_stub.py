"""A late tool result is omitted with its Step and never pins the cold-fold cut."""

from __future__ import annotations

import pytest

from history.types import (
    PORTABLE_HISTORY_FORMAT_VERSION,
    HistoryBoundary,
    PortableHistoryProjection,
    PortableMessage,
    PortableTurn,
)

THREAD = "fold-synthetic-stub-thread"


def _message(seq: int, role: str, content: str, **values) -> PortableMessage:
    return PortableMessage(
        message_id=f"message-{seq}",
        seq=seq,
        turn_id="turn-1",
        role=role,
        content=content,
        created_at=f"2026-09-17T00:00:{seq:02d}Z",
        **values,
    )


def _call(call_id: str, arguments: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": f"{call_id}_tool", "arguments": arguments},
    }


def _source(*, late_result_synthetic: bool) -> PortableHistoryProjection:
    """One unfinished Turn whose first call's result was settled at Turn end.

    Messages are in projection order: the settled result follows its Step even
    though its durable seq (7) is the Turn's last.
    """
    late = _message(
        7,
        "tool",
        "LATE RESULT BODY",
        assistant_step_id="message-2",
        tool_call_id="call-old",
        tool_name="call-old_tool",
        is_error=late_result_synthetic,
        synthetic_tool_result=late_result_synthetic,
        synthetic_reason="provider_missing_result" if late_result_synthetic else None,
    )
    messages = (
        _message(1, "user", "ORIGINAL ASK"),
        _message(2, "assistant", "OLD STEP", tool_calls=(_call("call-old", "x" * 20_000),)),
        late,
        _message(3, "assistant", "MIDDLE STEP", tool_calls=(_call("call-mid", "m" * 20_000),)),
        _message(4, "tool", "MIDDLE RESULT", assistant_step_id="message-3",
                 tool_call_id="call-mid", tool_name="call-mid_tool"),
        _message(5, "assistant", "LATEST STEP", tool_calls=(_call("call-new", "small"),)),
        _message(6, "tool", "LATEST RESULT", assistant_step_id="message-5",
                 tool_call_id="call-new", tool_name="call-new_tool"),
    )
    boundary = HistoryBoundary(THREAD, 1, 7, "message-7")
    turn = PortableTurn(
        turn_id="turn-1", status="completed", messages=messages, boundary=boundary,
    )
    return PortableHistoryProjection(
        format_version=PORTABLE_HISTORY_FORMAT_VERSION,
        thread_id=THREAD,
        history_epoch=1,
        start_exclusive=None,
        sealed_boundary=boundary,
        turns=(turn,),
        full_raw_replay_available=True,
    )


@pytest.mark.parametrize("late_result_synthetic", (True, False))
def test_late_result_is_omitted_with_its_step(late_result_synthetic) -> None:
    """The cut is by Step in delivery order, so a late seq cannot block it."""
    from core.fork_fit import render_cold_fold_input

    rendered = render_cold_fold_input(
        _source(late_result_synthetic=late_result_synthetic), max_input_tokens=800,
    )
    text = "\n".join(message["content"] for message in rendered.messages)

    assert rendered.first_included_seq == 5
    assert rendered.omitted_units == 2
    assert "LATEST STEP" in text and "LATEST RESULT" in text
    # The omitted call's settled result is omitted with it: no orphan result.
    assert "LATE RESULT BODY" not in text
    assert "call-old" not in text and "OLD STEP" not in text
