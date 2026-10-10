"""Claude Code writes one transcript line per content block and per tool result.

A parallel batch therefore lands as several assistant lines sharing one
``message.id`` and one user line per ``tool_result``. The warm tool-boundary
cut must hold for that shape whatever order the result lines were written in.
"""

from __future__ import annotations

import json

import pytest

from core import checkpoint_cc_boundary as boundary


def _lines(*records: dict) -> bytes:
    return b"".join(
        json.dumps(record, separators=(",", ":")).encode() + b"\n"
        for record in records
    )


def _assistant_block(message_id: str, block: dict) -> dict:
    return {"type": "assistant", "message": {"id": message_id, "content": [block]}}


def _result_line(tool_id: str) -> dict:
    return {"type": "user", "message": {"content": [{
        "type": "tool_result", "tool_use_id": tool_id, "content": f"result {tool_id}",
    }]}}


def _transcript(calls: tuple[str, ...], written: tuple[str, ...]) -> tuple[bytes, bytes]:
    previous = [
        _assistant_block("msg-previous", {"type": "text", "text": "running"}),
        *(
            _assistant_block("msg-previous", {"type": "tool_use", "id": call, "name": "Bash"})
            for call in calls
        ),
        *(_result_line(call) for call in written),
    ]
    current = _assistant_block(
        "msg-current", {"type": "tool_use", "id": "toolu-current", "name": "mcp__corsia__x"},
    )
    return _lines(*previous), _lines(*previous, current)


def _durable_boundary(calls: tuple[str, ...], last_saved: str):
    return boundary.ClaudeTranscriptBoundary(
        role="tool",
        content=f"result {last_saved}",
        tool_call_id=last_saved,
        message_id=f"row-{last_saved}",
        assistant_step_id="step-previous",
        tool_call_ids=calls,
    )


def test_single_call_boundary_cuts_before_the_current_step() -> None:
    prefix, transcript = _transcript(("toolu-a",), ("toolu-a",))

    assert boundary.cc_tool_boundary_prefix_bytes(
        transcript, "toolu-current", _durable_boundary(("toolu-a",), "toolu-a"),
    ) == len(prefix)


@pytest.mark.parametrize("last_saved", ("toolu-a", "toolu-b", "toolu-c"))
@pytest.mark.parametrize(
    "written",
    (
        ("toolu-a", "toolu-b", "toolu-c"),
        ("toolu-c", "toolu-a", "toolu-b"),
    ),
)
def test_parallel_batch_boundary_cuts_whatever_the_result_line_order(
    written: tuple[str, ...], last_saved: str,
) -> None:
    calls = ("toolu-a", "toolu-b", "toolu-c")
    prefix, transcript = _transcript(calls, written)

    assert boundary.cc_tool_boundary_prefix_bytes(
        transcript, "toolu-current", _durable_boundary(calls, last_saved),
    ) == len(prefix)


def test_parallel_batch_missing_a_result_is_not_a_boundary() -> None:
    calls = ("toolu-a", "toolu-b", "toolu-c")
    _prefix, transcript = _transcript(calls, ("toolu-c", "toolu-a"))

    assert boundary.cc_tool_boundary_prefix_bytes(
        transcript, "toolu-current", _durable_boundary(calls, "toolu-a"),
    ) is None


def test_parallel_batch_followed_by_other_input_is_not_its_boundary() -> None:
    """A later user line belongs after the durable row, so the cut must refuse."""
    calls = ("toolu-a", "toolu-b")
    prefix, _transcript_bytes = _transcript(calls, ("toolu-b", "toolu-a"))
    steer = {"type": "user", "uuid": "steer", "message": {"content": "also check logs"}}
    current = _assistant_block(
        "msg-current", {"type": "tool_use", "id": "toolu-current", "name": "mcp__corsia__x"},
    )

    assert boundary.cc_tool_boundary_prefix_bytes(
        prefix + _lines(steer, current),
        "toolu-current",
        _durable_boundary(calls, "toolu-b"),
    ) is None
