"""Boundary selection and per-decision assistant step batch reuse."""
from __future__ import annotations

import pytest

from history.types import HistoryBoundary, PortableHistoryError, PortableHistoryProjection, PortableMessage, PortableTurn
from subagents import harness_checkpoint_context as checkpoint


def _projection(case):
    def row(seq, role, turn="current", **kwargs):
        return PortableMessage(f"m{seq}", seq, turn, role, f"content {seq}", "t", **kwargs)

    previous = PortableTurn("previous", "completed", (row(1, "user", "previous"), row(2, "assistant", "previous")),
                            HistoryBoundary("thread", 0, 2, "m2"))
    messages = [row(3, "user"), row(4, "assistant", provider_message_id="p"),
                row(5, "tool", assistant_step_id="m4", tool_call_id="call")]
    if case == "no-assistant":
        messages = [row(3, "user")]
    elif case == "late-tool":
        messages = [row(3, "user"), row(4, "assistant"), row(6, "tool", assistant_step_id="m4"), row(5, "assistant")]
    elif case == "several-steps":
        messages = [row(3, "user"), row(4, "assistant"), row(5, "assistant")]
    elif case == "ownerless-tool":
        messages = [row(3, "user"), row(4, "tool"), row(5, "assistant")]
    elif case == "provider-group":
        messages = [row(3, "user"), row(4, "assistant", provider_message_id="p"),
                    row(5, "assistant", provider_message_id="p")]
    end = max(messages, key=lambda m: m.seq)
    boundary = HistoryBoundary("thread", 0, end.seq, end.message_id)
    status = "completed" if case == "terminal" else "running"
    current = PortableTurn("current", status, tuple(messages), boundary)
    turns = () if case == "empty" else (previous, current)
    start = previous.boundary if case in {"scoped", "bad-floor"} else None
    if start is not None:
        turns = (current,)
    return PortableHistoryProjection(1, "thread", 0, start, boundary if turns else None, turns, True), start


@pytest.mark.parametrize("case, expected", [
    pytest.param("empty", None, id="empty-history"),
    pytest.param("running", 2, id="running-turn-keeps-previous-turn"),
    pytest.param("terminal", 3, id="completed-turn-cuts-before-step"),
    pytest.param("scoped", 3, id="scoped-running-turn-keeps-input"),
    pytest.param("no-assistant", 2, id="running-without-step-keeps-previous-turn"),
    pytest.param("late-tool", 2, id="late-tool-prevents-interior-cut"),
    pytest.param("several-steps", 4, id="multiple-steps-cut-before-latest"),
    pytest.param("provider-group", 2, id="same-provider-message-stays-together"),
])
def test_explicit_boundaries(case, expected):
    projection, floor = _projection(case)
    result = checkpoint._external_boundary(projection, floor, current_turn_id="current")
    assert result == (HistoryBoundary("thread", 0, expected, f"m{expected}") if expected else None)


@pytest.mark.parametrize("case, message", [
    pytest.param("bad-floor", "a scoped projection must be floored at its start", id="scoped-floor-mismatch"),
    pytest.param("ownerless-tool", "canonical fold unit begins with an ownerless tool result", id="ownerless-tool"),
])
def test_invalid_history_errors(case, message):
    projection, _ = _projection(case)
    with pytest.raises(PortableHistoryError, match=message):
        checkpoint._external_boundary(projection, None, current_turn_id="current")


def test_later_completed_turn_uses_sealed_boundary():
    projection, floor = _projection("terminal")
    assert checkpoint._external_boundary(projection, floor, current_turn_id="earlier") == HistoryBoundary("thread", 0, 5, "m5")


def test_later_running_turn_has_no_external_boundary():
    projection, floor = _projection("running")
    assert checkpoint._external_boundary(projection, floor, current_turn_id="earlier") is None


def test_batches_are_reused_only_within_one_decision(monkeypatch):
    import history.turns as turns

    projection, floor = _projection("running")
    calls = []
    original = turns.assistant_step_batches

    def counted(turn):
        calls.append(turn.turn_id)
        return original(turn)

    monkeypatch.setattr(turns, "assistant_step_batches", counted)
    checkpoint._external_boundary(projection, floor, current_turn_id="current")
    assert calls == ["current", "previous"]
    checkpoint._external_boundary(projection, floor, current_turn_id="current")
    assert calls == ["current", "previous", "current", "previous"]
