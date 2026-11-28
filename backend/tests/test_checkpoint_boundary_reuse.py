"""Differential coverage for one checkpoint decision's batch reuse."""
from __future__ import annotations

import dataclasses
import json

import pytest

from history.turns import assistant_step_batches, turn_interior_cut_points
from history.types import HistoryBoundary, PortableHistoryProjection, PortableMessage, PortableTurn
from subagents import harness_checkpoint_context as checkpoint

DISCOVERED_MODELS = {"claude-code": ("claude-code/test",), "codex": ("codex/test",)}


def _old_external_boundary(projection, floor, *, current_turn_id=None, step_precedes_start=False):
    """The pre-change decision, retained as an independent differential oracle."""
    from history.source import _TERMINAL_TURN_STATES
    from history.types import PortableHistoryError

    latest = projection.turns[-1] if projection.turns else None
    if current_turn_id is not None and latest is not None and latest.turn_id != current_turn_id:
        if latest.status in _TERMINAL_TURN_STATES:
            return checkpoint._newer_boundary(projection.sealed_boundary, floor)
        return None
    running = latest is not None and latest.status == "running"
    batches = assistant_step_batches(latest) if running else ()
    if running and not batches:
        boundary = None
    else:
        if projection.start_exclusive is not None and floor != projection.start_exclusive:
            raise PortableHistoryError("a scoped projection must be floored at its start")
        newest = next((b[-1] for turn in reversed(projection.turns)
                       if (b := assistant_step_batches(turn))), None)
        if newest is None and step_precedes_start:
            boundary = None
        elif newest is None:
            boundary = projection.sealed_boundary
        else:
            provider_id = next((m.provider_message_id for m in newest.assistant_and_tools
                                if m.role == "assistant" and m.provider_message_id), None)
            group = (tuple(m for m in projection.messages if m.provider_message_id == provider_id)
                     if provider_id is not None else newest.assistant_and_tools)
            first_seq = min(m.seq for m in group)
            legal = {b for turn in projection.turns for b in (
                turn.boundary, *turn_interior_cut_points(turn),
                *(batch.resume_anchor for batch in assistant_step_batches(turn)
                  if batch.resume_anchor is not None),
            )}
            if projection.start_exclusive is not None:
                legal.add(projection.start_exclusive)
            candidate = max((b for b in legal if b.seq < first_seq),
                            key=lambda b: b.seq, default=None)
            boundary = checkpoint._newer_boundary(candidate, floor)
    own_input = boundary is not None and any(
        m.role == "user" and m.seq == boundary.seq and m.message_id == boundary.message_id
        for m in (latest.messages if latest is not None else ())
    )
    if running and (not batches or own_input):
        previous = projection.turns[-2].boundary if len(projection.turns) > 1 else None
        return checkpoint._newer_boundary(previous, floor) or boundary
    return boundary



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


@pytest.mark.parametrize("case", ["empty", "running", "terminal", "scoped", "bad-floor", "no-assistant", "late-tool", "ownerless-tool", "provider-group"])
@pytest.mark.parametrize("harness", ["codex", "claude-code"])
def test_boundary_matches_prechange(case, harness, monkeypatch):
    projection, floor = _projection(case)
    if case == "bad-floor":
        floor = None
    def run():
        return checkpoint._harness_boundaries(harness, projection, floor, current_turn_id="current")
    def capture():
        try:
            value = run()
            return {"value": [dataclasses.asdict(v) if v else None for v in value]}
        except Exception as error:
            return {"error": type(error).__name__, "message": str(error)}
    actual = capture()
    assert ("error" in actual) == (case in {"bad-floor", "ownerless-tool"})
    monkeypatch.setattr(checkpoint, "_external_boundary", _old_external_boundary)
    assert json.dumps(actual, sort_keys=True).encode() == json.dumps(capture(), sort_keys=True).encode()


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


def _encode(value):
    import sqlite3
    if dataclasses.is_dataclass(value):
        return {field.name: _encode(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, sqlite3.Row):
        return {key: _encode(value[key]) for key in value.keys()}
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    return value


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
@pytest.mark.parametrize("case", ["empty", "running", "terminal", "scoped", "bad-floor", "no-assistant", "late-tool", "ownerless-tool", "provider-group"])
def test_full_context_cases_match_prechange(signed_in_defaults, monkeypatch, harness, case):
    from infra.database import get_db
    from test_checkpoint_external_resume import _seed_completed_turn_without_current_input, _target

    target = _target(harness)
    db = get_db()
    _seed_completed_turn_without_current_input(db, "differential", target.catalog_id)
    db.close()
    monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda *_a, **_kw: target)
    monkeypatch.setattr("subagents.codex_runtime_support.resolve_working_dir", lambda *_a, **_kw: "/tmp")
    projection, floor = _projection(case)
    if case == "bad-floor":
        floor = None
    monkeypatch.setattr(checkpoint, "_read_history", lambda *_a, **_kw: (projection, floor, False))

    def capture():
        try:
            result = checkpoint.read_external_checkpoint_context(
                "differential", target.catalog_id, harness, current_turn_id="current",
            )
            assert result is not None
            return {"value": _encode(result)}
        except Exception as error:
            return {"error": type(error).__name__, "message": str(error)}

    actual = capture()
    assert ("error" in actual) == (case in {"bad-floor", "ownerless-tool"})
    monkeypatch.setattr(checkpoint, "_external_boundary", _old_external_boundary)
    assert json.dumps(actual, sort_keys=True).encode() == json.dumps(capture(), sort_keys=True).encode()


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
@pytest.mark.parametrize("exact", [False, True])
def test_full_context_matches_prechange(signed_in_defaults, monkeypatch, harness, exact):
    from infra.database import get_db
    from test_checkpoint_external_resume import _seed_completed_turn_without_current_input, _target

    target = _target(harness)
    thread_id = "differential"
    db = get_db()
    _seed_completed_turn_without_current_input(db, thread_id, target.catalog_id)
    db.close()
    monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda *_a, **_kw: target)
    monkeypatch.setattr("subagents.codex_runtime_support.resolve_working_dir", lambda *_a, **_kw: "/tmp")

    def capture():
        context = checkpoint.read_external_checkpoint_context(
            thread_id, target.catalog_id, harness, current_turn_id=None, exact_history=exact,
        )
        assert context is not None
        return json.dumps(_encode(context), sort_keys=True, separators=(",", ":")).encode()

    actual = capture()
    monkeypatch.setattr(checkpoint, "_external_boundary", _old_external_boundary)
    assert actual == capture()
