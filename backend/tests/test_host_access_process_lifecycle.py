from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_native_stop_cleans_exact_turn_then_persists_completed_tool_result(
    monkeypatch,
) -> None:
    from core import native_turn_control, native_turn_runtime
    from core.turn_core import TurnIdentity, TurnTerminalResult
    from host_access import processes
    from infra import state
    from infra.thread_driver import TerminalSettlement

    events: list[object] = []
    identity = TurnIdentity("native-turn", 7)
    cancel = native_turn_control.TurnCancelEvent()
    cancel.bind_turn_identity(identity)

    class _ProcessOwner:
        async def terminate_turn(
            self,
            thread_id: str,
            turn_id: str,
            *,
            foreground_only: bool,
            reason: str,
        ):
            assert cancel.is_set() is True
            assert llm_task.cancelling()
            events.append(
                (
                    "process_cleanup",
                    thread_id,
                    turn_id,
                    foreground_only,
                    reason,
                )
            )
            return ()

    class _Handler:
        async def handle(self, kind: str, payload: dict) -> None:
            events.append((kind, payload))

    async def run_turn_with_steps(*_args, on_step, cancel_event, **_kwargs):
        await cancel_event.wait()
        await on_step("tool_result", {"content": "terminated"})
        await on_step("turn_cancelled", {"reason": "user"})

    async def commit_terminal(turn_id, outcome, error, *, quarantine):
        assert (turn_id, outcome, error, quarantine) == ("native-turn", "cancelled", None, None)
        events.append("terminal_persist")
        return TerminalSettlement(TurnTerminalResult.APPLIED, "native-session")

    monkeypatch.setattr(processes, "PROCESS_OWNER", _ProcessOwner())
    monkeypatch.setattr(
        native_turn_runtime,
        "run_turn_with_steps",
        run_turn_with_steps,
    )
    monkeypatch.setattr(
        native_turn_runtime,
        "commit_turn_terminal",
        commit_terminal,
    )
    monkeypatch.setattr(
        native_turn_runtime,
        "drive_turn_settlement",
        lambda _settlement: asyncio.sleep(0),
    )
    monkeypatch.setattr(state, "_active_turns", {"native-session": cancel})
    llm_task = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(state, "_active_llm_tasks", {"native-session": llm_task})
    monkeypatch.setattr(state, "_active_turn_tasks", {})

    turn = native_turn_runtime.NativeTurn(
        agent=SimpleNamespace(),
        thread_id="native-session",
        content="run",
        model="test/model",
        turn_provider={},
        handler=_Handler(),
        cancel_event=cancel,
        identity=identity,
    )
    task = asyncio.create_task(native_turn_runtime.run_tracked_turn(turn))
    state._active_turn_tasks["native-session"] = task

    assert await native_turn_control.cancel_and_join_native_turn(
        "native-session"
    ) is True
    assert await task is True
    with pytest.raises(asyncio.CancelledError):
        await llm_task
    assert events.count((
        "process_cleanup",
        "native-session",
        "native-turn",
        True,
        "native_turn_interrupted",
    )) == 1
    assert [
        event
        for event in events
        if not (isinstance(event, tuple) and event[0] == "process_cleanup")
    ] == [
        ("tool_result", {"content": "terminated"}),
        "terminal_persist",
        ("turn_cancelled", {"reason": "user", "_replacement_requested": True}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel_event",
    [
        pytest.param(asyncio.Event(), id="missing"),
        pytest.param(
            SimpleNamespace(
                turn_identity=object(),
                completion=None,
                set=lambda: None,
            ),
            id="invalid",
        ),
    ],
)
async def test_active_native_stop_requires_exact_turn_identity(
    monkeypatch,
    cancel_event,
) -> None:
    from core import native_turn_control
    from infra import state

    monkeypatch.setattr(state, "_active_turns", {"native-session": cancel_event})
    monkeypatch.setattr(state, "_active_llm_tasks", {})
    monkeypatch.setattr(state, "_active_turn_tasks", {})

    with pytest.raises(
        RuntimeError,
        match="active turn on session native-session has no exact turn identity",
    ):
        await native_turn_control.cancel_and_join_native_turn("native-session")

    if isinstance(cancel_event, asyncio.Event):
        assert cancel_event.is_set() is False


@pytest.mark.asyncio
async def test_native_process_cleanup_finishes_before_caller_cancellation_propagates(
    monkeypatch,
    tmp_path,
) -> None:
    from core import native_turn_control
    from core.turn_core import TurnIdentity
    from host_access import processes
    from infra import state

    identity = TurnIdentity("native-turn", 7)
    cancel = native_turn_control.TurnCancelEvent()
    cancel.bind_turn_identity(identity)
    owner = processes.ProcessOwner(
        runtime_dir=tmp_path,
        term_grace_seconds=0.05,
        kill_grace_seconds=1.0,
    )
    ready = tmp_path / "native-process-ready"
    spawn_task = asyncio.create_task(owner.spawn(
        [
            sys.executable,
            "-c",
            (
                "import pathlib,signal,time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                f"pathlib.Path({str(ready)!r}).write_text('ready');"
                "time.sleep(30)"
            ),
        ],
        thread_id="native-session",
        turn_id=identity.turn_id,
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=30.0,
        background=False,
    ))
    deadline = asyncio.get_running_loop().time() + 2.0
    while not ready.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("Native foreground process did not start")
        await asyncio.sleep(0.01)
    llm_task = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(processes, "PROCESS_OWNER", owner)
    monkeypatch.setattr(state, "_active_turns", {"native-session": cancel})
    monkeypatch.setattr(state, "_active_llm_tasks", {"native-session": llm_task})
    monkeypatch.setattr(state, "_active_turn_tasks", {})

    try:
        stop_task = asyncio.create_task(
            native_turn_control.cancel_and_join_native_turn("native-session"),
        )
        while not cancel.is_set():
            await asyncio.sleep(0)
        assert llm_task.cancelling()

        stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop_task

        assert owner.live_count() == 0
        assert owner.running_records() == ()
        await spawn_task
        with pytest.raises(asyncio.CancelledError):
            await llm_task
    finally:
        if not spawn_task.done():
            spawn_task.cancel()
        await asyncio.gather(spawn_task, return_exceptions=True)
        await owner.shutdown()


@pytest.mark.asyncio
async def test_cc_admitted_bash_cancellation_converges_on_process_owner_cleanup(
    tmp_path,
) -> None:
    from core.turn_core import TurnIdentity
    from host_access.processes import ProcessOwner
    from subagents.cc_runtime_quiescence import quiesce_live_tools
    from subagents.cc_runtime_state import _Live

    owner = ProcessOwner(
        runtime_dir=tmp_path,
        term_grace_seconds=0.05,
        kill_grace_seconds=1.0,
    )
    identity = TurnIdentity("cc-turn", 9)
    live = _Live(
        session=SimpleNamespace(mcp_config=None),
        parent_thread_id=None,
        name="cc",
        corsia_turn=identity,
        turn_active=True,
        tool_registry=object(),
        tool_token="secret",
    )

    async def admitted_bash() -> None:
        task = asyncio.current_task()
        assert task is not None
        live.admitted_tool_tasks.add(task)
        try:
            await owner.spawn(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                thread_id="cc-session",
                turn_id=identity.turn_id,
                cwd=Path.cwd(),
                env=os.environ,
                timeout_seconds=30.0,
                background=False,
            )
        finally:
            live.admitted_tool_tasks.discard(task)

    task = asyncio.create_task(admitted_bash())
    deadline = asyncio.get_running_loop().time() + 2.0
    while owner.live_count() != 1:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("CC Bash process did not start")
        await asyncio.sleep(0.01)

    assert await quiesce_live_tools(
        "cc-session",
        live,
        identity,
        timeout_s=2.0,
    )
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.live_count() == 0
    assert owner.running_records() == ()
    await owner.shutdown()
