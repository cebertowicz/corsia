"""Stop reaches a Native provider call that registers after Stop signalled.

Stop sets the Turn's cancel event and then cancels whichever provider task it
finds. A provider call that registers after that moment was found by nobody:
the Turn waited on the model while Stop's join expired, the user saw
``Stop failed:`` and the Turn kept running (#1558).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

THREAD = "stop-window-thread"


def _held_provider(started: asyncio.Event):
    """A provider whose reply never arrives on its own, like a held model call."""

    async def chat_completion(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    return chat_completion


async def _ignore_step(_kind, _payload):
    return None


@pytest.mark.asyncio
async def test_provider_call_registered_after_stop_is_cancelled_before_it_starts(monkeypatch):
    from core import turn_provider_call
    from infra import state

    started = asyncio.Event()
    monkeypatch.setattr(turn_provider_call, "chat_completion", _held_provider(started))
    cancel_event = asyncio.Event()
    cancel_event.set()

    result = await asyncio.wait_for(
        turn_provider_call.call_llm(
            SimpleNamespace(temperature=0), [], [], "mock/model", {},
            THREAD, cancel_event, _ignore_step, 2,
        ),
        timeout=2,
    )

    assert result == (None, False)
    assert not started.is_set()
    assert THREAD not in state._active_llm_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_requested", (False, True))
async def test_stop_during_checkpoint_maintenance_ends_the_turn_within_the_join(
    monkeypatch, replacement_requested,
):
    """Stop lands after the loop's cancel check and before the next provider call."""
    from core import turn_iteration, turn_provider_call
    from core import turn_iteration_context as iteration_context
    from core.native_turn_control import TurnCancelEvent, cancel_and_join_native_turn
    from core.turn_core import TurnIdentity
    from infra import state

    started = asyncio.Event()
    in_maintenance = asyncio.Event()
    finish_maintenance = asyncio.Event()

    async def maintain_native_checkpoint(**_kwargs):
        in_maintenance.set()
        await finish_maintenance.wait()
        return None

    monkeypatch.setattr(turn_provider_call, "chat_completion", _held_provider(started))
    monkeypatch.setattr(iteration_context, "maintain_native_checkpoint", maintain_native_checkpoint)
    frames: list[str] = []

    async def on_step(kind, _payload):
        frames.append(kind)

    identity = TurnIdentity("stop-window-turn", 1)
    cancel_event = TurnCancelEvent()
    cancel_event.bind_turn_identity(identity)
    loop_state = turn_iteration._IterationState(
        messages=[{"role": "user", "content": "read the probe"}],
        context_usage={}, tool_definitions=[], context_identity=None, fit_ledger=None,
        fit_current_input="read the probe", checkpoint_source_messages=[],
        turn_model="mock/model", iteration=1,
    )
    agent = SimpleNamespace(max_iterations=5, temperature=0)
    turn = asyncio.ensure_future(turn_iteration._run_loop(
        agent, loop_state, THREAD, "mock/model", {}, cancel_event, on_step,
        None, None, None, identity, None,
    ))
    turn.add_done_callback(lambda _done: cancel_event.completion.set_result(True))
    state._active_turns[THREAD] = cancel_event
    state._active_turn_tasks[THREAD] = turn
    try:
        await asyncio.wait_for(in_maintenance.wait(), timeout=2)
        stop = asyncio.ensure_future(cancel_and_join_native_turn(
            THREAD, timeout_s=1.0, expected_turn=identity,
            replacement_requested=replacement_requested,
        ))
        await asyncio.wait_for(cancel_event.wait(), timeout=2)
        finish_maintenance.set()

        assert await stop is True
        assert turn.done()
        assert "turn_cancelled" in frames
        assert not started.is_set()
    finally:
        finish_maintenance.set()
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        state._active_turns.pop(THREAD, None)
        state._active_turn_tasks.pop(THREAD, None)
        state._active_llm_tasks.pop(THREAD, None)
