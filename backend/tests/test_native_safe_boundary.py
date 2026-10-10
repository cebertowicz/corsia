from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core import (
    turn_iteration,
    turn_iteration_context,
    turn_provider_call,
    turns,
    turns_steps,
)


def _response(*, tool_calls: bool, content: str) -> SimpleNamespace:
    return SimpleNamespace(
        has_tool_calls=tool_calls,
        finish_reason="stop",
        content=content,
        usage={"prompt_tokens": 10},
        reasoning_content=None,
        reasoning_provenance=None,
        ttft_ms=None,
    )

@pytest.mark.asyncio
async def test_native_restart_marks_intent_without_hard_cancelling_active_step():
    from core.turn_core import TurnIdentity
    from core.native_turn_control import (
        TurnCancelEvent,
        cancel_native_restart,
        request_native_restart,
    )
    from infra import state

    thread_id = "native-restart-intent"
    turn_id = "turn-native-restart-intent"
    handle = TurnCancelEvent()
    handle.bind_turn_identity(TurnIdentity(turn_id, 1))
    state._active_turns[thread_id] = handle
    try:
        assert request_native_restart(thread_id, turn_id, "restart pending")
        assert handle.restart_pending is True
        assert handle.restart_pending_message == "restart pending"
        assert not handle.is_set()
        assert cancel_native_restart(thread_id, turn_id, "restart cancelled")
        assert handle.restart_pending is False
        assert handle.restart_pending_message is None
        assert not handle.is_set()
    finally:
        state._active_turns.pop(thread_id, None)


@pytest.mark.asyncio
async def test_native_restart_stops_after_inflight_tool_batch_and_registers_wake(
    monkeypatch,
):
    from core.turn_core import TurnIdentity
    from core.native_turn_control import TurnCancelEvent

    calls: list[list[dict]] = []
    registrations: list[tuple[str, str]] = []
    cancel_event = TurnCancelEvent()
    cancel_event.bind_turn_identity(TurnIdentity("turn-restart", 1))

    async def fake_call(*args, **_kwargs):
        calls.append([dict(message) for message in args[1]])
        return _response(tool_calls=True, content="working"), False

    async def fake_execute(*_args, **_kwargs):
        cancel_event.restart_pending = True
        cancel_event.restart_pending_message = "restart pending"
        cancel_event.restart_cancelled_message = None
        return [
            {"role": "assistant", "content": "working", "tool_calls": []},
            {"role": "tool", "tool_call_id": "tool-1", "content": "finished"},
        ]

    async def run_in_db(fn, *args, **_kwargs):
        if fn.__name__ == "register_restart_resume":
            registrations.append((args[0], args[1]))
        return None

    events: list[object] = []

    await _run_boundary_turn(
        monkeypatch,
        fake_call=fake_call,
        fake_execute=fake_execute,
        safe_boundary=lambda _remaining: None,
        events=events,
        cancel_event=cancel_event,
        run_in_db=run_in_db,
    )

    assert len(calls) == 1
    assert events == ["context_usage", "turn_cancelled"]
    assert registrations == [("thread-1", "turn-restart")]


@pytest.mark.asyncio
async def test_native_restart_does_not_admit_tools_returned_after_intent(
    monkeypatch,
):
    from core.turn_core import TurnIdentity
    from core.native_turn_control import TurnCancelEvent

    calls = 0
    executed = False
    registrations: list[tuple[str, str]] = []
    cancel_event = TurnCancelEvent()
    cancel_event.bind_turn_identity(TurnIdentity("turn-provider-boundary", 2))

    async def fake_call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        cancel_event.restart_pending = True
        return _response(tool_calls=True, content="proposed work"), False

    async def fake_execute(*_args, **_kwargs):
        nonlocal executed
        executed = True
        return []

    async def run_in_db(fn, *args, **_kwargs):
        if fn.__name__ == "register_restart_resume":
            registrations.append((args[0], args[1]))
        return None

    events: list[object] = []
    await _run_boundary_turn(
        monkeypatch,
        fake_call=fake_call,
        fake_execute=fake_execute,
        safe_boundary=lambda _remaining: None,
        events=events,
        cancel_event=cancel_event,
        run_in_db=run_in_db,
    )

    assert calls == 1
    assert executed is False
    assert events == ["context_usage", "turn_cancelled"]
    assert registrations == [("thread-1", "turn-provider-boundary")]


@pytest.mark.asyncio
async def test_native_restart_allows_current_final_response_to_complete(monkeypatch):
    from core.turn_core import TurnIdentity
    from core.native_turn_control import TurnCancelEvent

    registrations: list[tuple[str, str]] = []
    cancel_event = TurnCancelEvent()
    cancel_event.bind_turn_identity(TurnIdentity("turn-final", 2))

    async def fake_call(*_args, **_kwargs):
        cancel_event.restart_pending = True
        cancel_event.restart_pending_message = "restart pending"
        return _response(tool_calls=False, content="done"), False

    async def unexpected_execute(*_args, **_kwargs):
        raise AssertionError("a final response has no tool batch")

    async def run_in_db(fn, *args, **_kwargs):
        if fn.__name__ == "register_hard_recovery":
            registrations.append((args[0], args[1]))
        return None

    events: list[object] = []
    await _run_boundary_turn(
        monkeypatch,
        fake_call=fake_call,
        fake_execute=unexpected_execute,
        safe_boundary=lambda _remaining: None,
        events=events,
        cancel_event=cancel_event,
        run_in_db=run_in_db,
    )

    assert events == ["context_usage", "text", "turn_complete"]
    assert registrations == []


@pytest.mark.asyncio
async def test_native_restart_cancel_racing_resume_registration_continues_turn(
    monkeypatch,
):
    """Rollback winning the registration race is not a Turn failure."""
    from core.turn_core import TurnIdentity
    from core.native_turn_control import TurnCancelEvent

    cancel_event = TurnCancelEvent()
    cancel_event.bind_turn_identity(TurnIdentity("turn-raced-cancel", 3))
    cancel_event.restart_pending = True
    calls = 0

    async def fake_call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _response(tool_calls=False, content="continued"), False

    async def unexpected_execute(*_args, **_kwargs):
        raise AssertionError("continued final response has no tools")

    async def run_in_db(fn, *_args, **_kwargs):
        if fn.__name__ == "register_restart_resume":
            # Model abort_restart_control clearing intent and cancelling the
            # controlled DB row before the worker applies controlled -> pending.
            cancel_event.restart_pending = False
            raise RuntimeError("restart control is not ready for resume registration")
        return None

    events: list[object] = []
    await _run_boundary_turn(
        monkeypatch,
        fake_call=fake_call,
        fake_execute=unexpected_execute,
        safe_boundary=lambda _remaining: None,
        events=events,
        cancel_event=cancel_event,
        run_in_db=run_in_db,
    )

    assert calls == 1
    assert events == ["context_usage", "text", "turn_complete"]
    assert not cancel_event.is_set()


@pytest.mark.asyncio
async def test_native_safe_boundary_inserts_after_tool_batch_before_next_call(
    monkeypatch,
):
    """A queued correction is causally between finished tools and inference."""
    from core.destination_preflight import attach_native_provider_fit_ledger
    from core.destination_fit import (
        _digest,
        require_provider_call_fit,
    )

    calls: list[dict] = []
    events: list[object] = []
    responses = iter([
        (_response(tool_calls=True, content="working"), False),
        (_response(tool_calls=False, content="done"), False),
    ])

    async def fake_call(*args, **kwargs):
        messages = [dict(message) for message in args[1]]
        tools = [dict(tool) for tool in args[2]]
        require_provider_call_fit(
            kwargs.get("destination_fit_ledger"),
            harness="native",
            target_catalog_id="native/test",
            accounting_payload={"messages": messages, "tools": tools},
            input_identity=kwargs.get("destination_fit_input_identity"),
            current_input=kwargs.get("destination_fit_current_input"),
        )
        calls.append({
            "messages": messages,
            "ledger": kwargs.get("destination_fit_ledger"),
            "input_identity": kwargs.get("destination_fit_input_identity"),
            "current_input": kwargs.get("destination_fit_current_input"),
            "tools": tools,
        })
        return next(responses)

    async def fake_execute(*_args, **_kwargs):
        return [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "initial"},
            {"role": "assistant", "content": "working", "tool_calls": []},
            {"role": "tool", "tool_call_id": "tool-1", "content": "finished"},
        ]

    async def safe_boundary(_max_input_tokens: int):
        return turn_iteration.NativeSafeBoundaryInput(
            content="change direction",
            input_identity="queued-message-1",
            before_provider_write=lambda: _record(events, "before"),
            settle_delivery=lambda: _record(events, "settled"),
            release_before_write=lambda: _record(events, "released"),
            mark_ambiguous=lambda: _record(events, "ambiguous"),
        )

    async def on_step(kind, _payload):
        events.append(kind)

    async def no_db(*_args, **_kwargs):
        return None

    monkeypatch.setattr(turn_provider_call, "call_llm", fake_call)
    monkeypatch.setattr(turns_steps, "execute_tool_calls", fake_execute)
    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", lambda _sid: no_db())
    monkeypatch.setattr(turn_iteration, "run_in_db", no_db)
    monkeypatch.setattr(turns_steps, "emit_reasoning", no_db)

    agent = SimpleNamespace(
        model="native/test",
        max_iterations=4,
        _connect_mcp=no_db,
        _strip_think=lambda value: value,
    )
    initial_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "initial"},
    ]
    initial_fit = attach_native_provider_fit_ledger(
        target_catalog_id="native/test",
        proven_target_window_tokens=100_000,
        messages=initial_messages,
        tools=[],
        input_identity="initial-message",
        # The send path digests the rendered FIFO head message, not the raw
        # user text, so the initial ledger must be built the way the turn body
        # reads it back.
        current_input=initial_messages[-1],
    )
    prepared = SimpleNamespace(
        messages=initial_messages,
        context_usage={},
        tool_definitions=[],
        context_identity=None,
        fit_ledger=initial_fit.fit_ledger,
    )

    await turns.run_turn_with_steps(
        agent,
        "initial",
        "thread-1",
        "web",
        "thread-1",
        on_step,
        asyncio.Event(),
        model="native/test",
        prepared_context=prepared,
        model_context_window=100_000,
        on_safe_boundary=safe_boundary,
    )

    assert [message["role"] for message in calls[1]["messages"][-3:]] == [
        "assistant",
        "tool",
        "user",
    ]
    assert calls[1]["messages"][-1] == {
        "role": "user",
        "content": "change direction",
    }
    assert calls[1]["ledger"] is not calls[0]["ledger"]
    assert calls[1]["ledger"].input_identity == "queued-message-1"
    assert calls[1]["input_identity"] == "queued-message-1"
    assert calls[1]["current_input"] == "change direction"
    assert calls[1]["ledger"].destination_payload_sha256 == _digest({
        "messages": calls[1]["messages"],
        "tools": calls[1]["tools"],
    })
    assert events.index("before") < events.index("settled") < events.index("text")
    assert "released" not in events
    assert "ambiguous" not in events


async def _record(events: list[object], value: str) -> None:
    events.append(value)


@pytest.mark.asyncio
async def test_native_safe_boundary_releases_claim_when_turn_stops_before_next_call(
    monkeypatch,
):
    """A Stop between a tool batch and inference leaves the inbox head queued."""
    events: list[object] = []
    cancel_event = asyncio.Event()

    async def fake_call(*_args, **_kwargs):
        return _response(tool_calls=True, content="working"), False

    async def fake_execute(*_args, **_kwargs):
        return []

    async def safe_boundary(_max_input_tokens: int):
        candidate = turn_iteration.NativeSafeBoundaryInput(
            content="queued correction",
            input_identity="queued-message-1",
            before_provider_write=lambda: _record(events, "before"),
            settle_delivery=lambda: _record(events, "settled"),
            release_before_write=lambda: _record(events, "released"),
            mark_ambiguous=lambda: _record(events, "ambiguous"),
        )
        cancel_event.set()
        return candidate

    await _run_boundary_turn(
        monkeypatch,
        fake_call=fake_call,
        fake_execute=fake_execute,
        safe_boundary=safe_boundary,
        events=events,
        cancel_event=cancel_event,
    )

    assert events == ["context_usage", "turn_cancelled", "released"]


@pytest.mark.asyncio
async def test_native_safe_boundary_marks_ambiguous_after_write_intent_before_failure(
    monkeypatch,
):
    """Once the next provider call is fenced, an unknown outcome cannot replay."""
    events: list[object] = []
    responses = iter([(_response(tool_calls=True, content="working"), False)])

    async def fake_call(*_args, **_kwargs):
        try:
            return next(responses)
        except StopIteration as exc:
            raise RuntimeError("provider transport failed") from exc

    async def fake_execute(*_args, **_kwargs):
        return []

    async def safe_boundary(_max_input_tokens: int):
        return turn_iteration.NativeSafeBoundaryInput(
            content="queued correction",
            input_identity="queued-message-1",
            before_provider_write=lambda: _record(events, "before"),
            settle_delivery=lambda: _record(events, "settled"),
            release_before_write=lambda: _record(events, "released"),
            mark_ambiguous=lambda: _record(events, "ambiguous"),
        )

    with pytest.raises(RuntimeError, match="provider transport failed"):
        await _run_boundary_turn(
            monkeypatch,
            fake_call=fake_call,
            fake_execute=fake_execute,
            safe_boundary=safe_boundary,
            events=events,
        )

    assert events == ["context_usage", "before", "error", "ambiguous"]
    assert "released" not in events


async def _run_boundary_turn(
    monkeypatch,
    *,
    fake_call,
    fake_execute,
    safe_boundary,
    events: list[object],
    cancel_event: asyncio.Event | None = None,
    run_in_db=None,
) -> None:
    """Run a minimal native turn with a caller-owned safe-boundary candidate."""
    from shared_send_fixtures import _insert_native_model

    _insert_native_model()

    async def on_step(kind, _payload):
        events.append(kind)

    async def no_db(*_args, **_kwargs):
        return None

    monkeypatch.setattr(turn_provider_call, "call_llm", fake_call)
    monkeypatch.setattr(turns_steps, "execute_tool_calls", fake_execute)
    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", lambda _sid: no_db())
    monkeypatch.setattr(turn_iteration, "run_in_db", run_in_db or no_db)
    monkeypatch.setattr(turns_steps, "emit_reasoning", no_db)

    agent = SimpleNamespace(
        model="native/test",
        max_iterations=4,
        _connect_mcp=no_db,
        _strip_think=lambda value: value,
    )
    from core.destination_preflight import attach_native_provider_fit_ledger

    initial_messages = [{"role": "system", "content": "system"}]
    prepared_fit = attach_native_provider_fit_ledger(
        target_catalog_id="native/test",
        proven_target_window_tokens=100_000,
        messages=initial_messages,
        tools=[],
        input_identity="initial-message",
        current_input="initial",
    )
    prepared = SimpleNamespace(
        messages=initial_messages,
        context_usage={},
        tool_definitions=[],
        context_identity=None,
        fit_ledger=prepared_fit.fit_ledger,
    )
    await turns.run_turn_with_steps(
        agent,
        "initial",
        "thread-1",
        "web",
        "thread-1",
        on_step,
        cancel_event or asyncio.Event(),
        model="native/test",
        prepared_context=prepared,
        model_context_window=100_000,
        on_safe_boundary=safe_boundary,
    )
