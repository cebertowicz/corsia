"""The step callback must propagate the handler's return value.

``StepHandler._handle_tool_complete`` persists the tool row and returns it as
the canonical ``PortableMessage``; the tool-projection step downstream
requires that row and fails the turn without it. ``run_tracked_turn``'s step
wrapper dropped the return, so every tool-using native turn failed with
"Native tool projection requires its canonical persisted row" — the first
tool call on a fresh thread was enough.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core import native_turn_runtime
from core.turn_core import (
    TurnIdentity,
    TurnTerminalOwnershipError,
    TurnTerminalResult,
)


@pytest.mark.asyncio
async def test_step_wrapper_returns_the_canonical_tool_row(monkeypatch) -> None:
    sentinel = SimpleNamespace(role="tool", message_id="tool-row-1")
    seen: dict = {}

    class Handler:
        async def handle(self, kind: str, payload: dict):
            seen[kind] = payload
            if kind == "tool_complete":
                return sentinel
            return None

    captured: dict = {}

    async def fake_run_turn_with_steps(*, on_step, **kwargs):
        # Drive the wrapper exactly as execute_tool_calls does and record
        # what a downstream projection step would receive.
        captured["canonical"] = await on_step(
            "tool_complete", {"id": "call-1", "name": "read_file", "result": "ok"},
        )
        captured["other"] = await on_step("text", {"content": "hi"})
        await on_step("turn_complete", {})

    monkeypatch.setattr(
        native_turn_runtime,
        "run_turn_with_steps",
        fake_run_turn_with_steps,
    )

    async def reject_terminal(*_args, **_kwargs):
        return SimpleNamespace(result=TurnTerminalResult.CONFLICTING_TERMINAL)

    monkeypatch.setattr(native_turn_runtime, "commit_turn_terminal", reject_terminal)

    identity = TurnIdentity("turn-1", 1)
    turn = native_turn_runtime.NativeTurn(
        agent=SimpleNamespace(),
        thread_id="thread-1",
        content="hello",
        model="native/test",
        turn_provider={},
        handler=Handler(),
        cancel_event=asyncio.Event(),
        identity=identity,
    )
    # The stubbed terminal owner rejects the CAS by design: the contract
    # under test is fully exercised inside run_turn_with_steps before any
    # terminal handling runs, so ownership rejection here is expected and
    # anything else must fail the test.
    with pytest.raises(TurnTerminalOwnershipError):
        await native_turn_runtime.run_tracked_turn(turn)

    assert captured["canonical"] is sentinel
    assert captured["other"] is None
    assert "tool_complete" in seen
