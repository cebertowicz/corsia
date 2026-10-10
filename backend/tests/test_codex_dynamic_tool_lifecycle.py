"""Codex dynamic-tool settlement and destructive-boundary regressions."""

from __future__ import annotations

from llm.context_window import WindowFacts
from automation import plan_liveness_terminal
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from codex_runtime_fixtures import (
    _FenceSession,
    _admit_active_turn,
    _active_live,
    _active_live_with_persisted_tool,
    _codex_target,
    _reset_runtime,
    locked_spawn,
)
from core.turn_core import (
    TurnIdentity,
    TurnTerminalResult,
    persist_turn_start,
    persist_turn_terminal,
)
from shared_send_fixtures import _insert_session
from subagents import codex_runtime, codex_runtime_events, codex_runtime_state


_TEST_HANG_TIMEOUT_S = 30


class _EmergencyStopSession:
    """A provider process whose exit and in-flight tool cleanup are controlled."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.codex_thread_id = f"provider-{thread_id}"
        self.active_turn_id = f"provider-turn-{thread_id}"
        self.returncode: int | None = None
        self._closing = False
        self.provider_exit = asyncio.Event()
        self.request_idle = asyncio.Event()
        self.kill_requested = asyncio.Event()
        self.calls: list[str] = []
        self.status_at_drain: str | None = None

    def _request_terminate(self) -> None:
        self.calls.append("provider_kill")
        self._closing = True
        self.kill_requested.set()

    def revoke_dynamic_tool_admission(self) -> None:
        self.calls.append("revoke")

    async def wait_dynamic_tool_requests_idle(self) -> None:
        self.calls.append("wait_request")
        await self.request_idle.wait()

    async def terminate(self) -> None:
        from infra.database import get_db

        self.calls.append("drain")
        with get_db() as db:
            row = db.execute(
                "SELECT status FROM active_turns WHERE thread_id=?",
                (self.thread_id,),
            ).fetchone()
        self.status_at_drain = None if row is None else row["status"]
        self.returncode = -9


async def _invoke_destructive(path: str, thread_id: str, live) -> object:
    if path == "hard_preempt":
        return await codex_runtime._hard_preempt_live_turn(
            thread_id,
            live,
            successor_turn_id=f"turn-replacement-{thread_id}",
            text="replacement",
            project_id=None,
            model="gpt-5-codex",
            held_lock=codex_runtime._LIVE.lock(thread_id),
        )
    if path == "stop":
        return await codex_runtime.stop(
            thread_id,
            expected_turn_id=live.corsia_turn.turn_id,
        )
    if path == "retire":
        return await codex_runtime.retire(thread_id)
    if path == "mark_dead":
        return await codex_runtime.mark_dead(thread_id)
    if path == "shutdown":
        return await codex_runtime.shutdown_all()
    raise AssertionError(path)


async def _deliver_terminal(on_event, live, event: dict) -> None:
    """Join deferred terminal work outside the JSON-RPC reader callback."""
    await on_event(event)
    assert live.delivery_event_tail is not None
    await live.delivery_event_tail


@pytest.mark.asyncio
async def test_stale_app_server_exit_settles_old_tool_debt_without_quarantining_successor(
    isolated_db,
):
    from infra.database import get_db
    from registry import models

    _reset_runtime()
    thread_id = "codex-stale-app-exit"
    old_session = _FenceSession("old")
    old_live, step_id = _active_live_with_persisted_tool(
        thread_id,
        old_session,
    )
    successor_session = _FenceSession("successor")
    successor = codex_runtime._LiveCodex(
        codex_thread_id="provider-successor",
        session=successor_session,
        parent_thread_id=None,
        name="codex",
    )
    codex_runtime._LIVE.put(thread_id, successor)
    codex_runtime._APP_SESSIONS[thread_id] = successor_session
    codex_runtime._CODEX_THREADS[thread_id] = successor.codex_thread_id
    models.register_artifact(
        integration="codex",
        type="thread",
        external_id=successor.codex_thread_id,
        parent_thread_id=thread_id,
    )

    await codex_runtime._finish_transport_exit(
        thread_id,
        old_live,
        old_session,
        7,
        "stale abnormal EOF",
        lambda _sid, _payload: None,
    )

    db = get_db()
    try:
        tool_row = db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND role='tool'",
            (thread_id,),
        ).fetchone()
        durable_thread = models.find_for_thread(
            thread_id, "codex", "thread", db=db,
        )
    finally:
        db.close()
    assert json.loads(tool_row["metadata"])["assistant_step_id"] == step_id
    assert codex_runtime._LIVE.get(thread_id) is successor
    assert codex_runtime._APP_SESSIONS.get(thread_id) is successor_session
    assert codex_runtime._CODEX_THREADS[thread_id] == successor.codex_thread_id
    assert thread_id not in codex_runtime._PROVIDER_CONTEXT_UNTRUSTED
    assert durable_thread["external_id"] == successor.codex_thread_id
    _reset_runtime()


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "keeps_resume"), [
    ("hard_preempt", False),
    ("retire", False),
    ("mark_dead", False),
    ("shutdown", True),
])
async def test_destructive_callers_share_request_result_terminal_fence(
    isolated_db, monkeypatch, path, keeps_resume,
):
    _reset_runtime()
    thread_id = f"codex-fence-{path}"
    session = _FenceSession(thread_id)
    live = _active_live(thread_id, session)
    monkeypatch.setattr(
        codex_runtime, "_spawn_turn", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        codex_runtime, "_retire_registered_thread_artifacts", lambda *_a, **_k: False,
    )
    monkeypatch.setattr(codex_runtime, "insert_message", lambda *_a, **_k: None)

    task = asyncio.create_task(_invoke_destructive(path, thread_id, live))
    try:
        while "wait_request" not in session.calls and not task.done():
            await asyncio.sleep(0)
        assert session.calls[:2] == ["revoke", "wait_request"]
        assert task.done() is False

        session.request_idle.set()
        await asyncio.sleep(0)
        assert "interrupt" not in session.calls
        assert task.done() is False

        live.tool_names.clear()
        live.tool_step_ids.clear()
        live.tools_settled.set()
        await asyncio.wait_for(task, timeout=_TEST_HANG_TIMEOUT_S)

        assert session.calls == [
            "revoke", "wait_request", "interrupt", "terminate",
        ]
        assert session.admission_open is False
        assert codex_runtime._LIVE.get(thread_id) is None
        assert thread_id not in codex_runtime._APP_SESSIONS
        assert (thread_id in codex_runtime._CODEX_THREADS) is keeps_resume
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        _reset_runtime()


@pytest.mark.asyncio
async def test_stop_settles_real_tool_debt_before_exact_runtime_retirement(
    isolated_db,
) -> None:
    from infra.database import get_db

    _reset_runtime()
    thread_id = "codex-stop-real-tool-debt"
    session = _FenceSession(thread_id)
    session._request_terminate = lambda: session.calls.append("provider_kill")
    live, step_id = _active_live_with_persisted_tool(thread_id, session)
    stopped = await codex_runtime.stop(
        thread_id, expected_turn_id=live.corsia_turn.turn_id,
    )
    assert stopped is True
    assert session.calls == ["provider_kill", "revoke", "terminate"]
    with get_db() as db:
        tool = db.execute(
            "SELECT metadata FROM messages "
            "WHERE thread_id=? AND role='tool'",
            (thread_id,),
        ).fetchone()
        turn = db.execute(
            "SELECT status FROM active_turns WHERE id=?",
            (live.corsia_turn.turn_id,),
        ).fetchone()
    tool_meta = json.loads(tool["metadata"])
    assert tool_meta["assistant_step_id"] == step_id
    assert tool_meta["synthetic_reason"] == "provider_missing_result"
    assert turn["status"] == "cancelled"
    assert live.tool_step_ids == {}
    assert live.tools_settled.is_set()
    assert codex_runtime._LIVE.get(thread_id) is None
    assert thread_id not in codex_runtime._APP_SESSIONS
    assert codex_runtime._CODEX_THREADS[thread_id] == live.codex_thread_id
    _reset_runtime()


@pytest.mark.asyncio
async def test_natural_completion_wins_interrupt_race(isolated_db, monkeypatch):
    _reset_runtime()
    thread_id = "codex-natural-terminal-race"
    session = _FenceSession(thread_id, request_idle=True)
    live = _active_live(thread_id, session, pending_tool=False)
    identity = live.corsia_turn

    async def interrupt(timeout: float = 30.0) -> bool:
        del timeout
        session.calls.append("interrupt")
        persist_turn_terminal(identity.turn_id, "completed")
        codex_runtime_state.finalize_live(thread_id, live)
        return True

    session.interrupt = interrupt
    monkeypatch.setattr(
        codex_runtime, "_spawn_turn", AsyncMock(return_value=None),
    )

    await codex_runtime._hard_preempt_live_turn(
        thread_id,
        live,
        successor_turn_id=f"turn-replacement-{thread_id}",
        text="replacement",
        project_id=None,
        model="gpt-5-codex",
        held_lock=codex_runtime._LIVE.lock(thread_id),
    )

    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
        ).fetchone()
    finally:
        db.close()
    assert row["status"] == "completed"
    assert session.calls == ["revoke", "wait_request", "interrupt", "terminate"]
    _reset_runtime()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["failed", "timeout", "cancelled"])
async def test_failed_fence_restores_admission_on_surviving_session(
    isolated_db, monkeypatch, failure,
):
    _reset_runtime()
    thread_id = f"codex-fence-{failure}"
    session = _FenceSession(thread_id, request_idle=failure == "failed")
    live = _active_live(thread_id, session, pending_tool=False)
    if failure == "failed":
        session.interrupt_result = False
    elif failure == "timeout":
        monkeypatch.setattr(codex_runtime, "_DYNAMIC_TOOL_DRAIN_TIMEOUT_S", 0.01)

    task = asyncio.create_task(codex_runtime._quiesce_app_server_tool_turn(
        thread_id, live, lambda _sid, _payload: None,
    ))
    if failure == "cancelled":
        while "wait_request" not in session.calls and not task.done():
            await asyncio.sleep(0)
        assert task.done() is False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task is False

    assert session.admission_open is True
    assert session.calls[-1] == "restore"
    assert codex_runtime._LIVE.get(thread_id) is live
    assert codex_runtime._APP_SESSIONS.get(thread_id) is session
    _reset_runtime()


@pytest.mark.asyncio
async def test_quiesce_stops_exact_foreground_group_before_waiting_for_request(
    isolated_db,
    monkeypatch,
) -> None:
    from host_access import processes

    _reset_runtime()
    thread_id = "codex-process-fence"
    session = _FenceSession(thread_id, request_idle=True)
    live = _active_live(thread_id, session, pending_tool=False)

    class _ProcessOwner:
        async def terminate_turn(
            self,
            exact_session_id: str,
            turn_id: str,
            *,
            foreground_only: bool,
            reason: str,
        ):
            assert exact_session_id == thread_id
            assert turn_id == live.corsia_turn.turn_id
            assert foreground_only is True
            assert reason == "codex_turn_interrupted"
            session.calls.append("process_cleanup")
            return ()

    monkeypatch.setattr(processes, "PROCESS_OWNER", _ProcessOwner())

    assert await codex_runtime._quiesce_app_server_tool_turn(
        thread_id,
        live,
        lambda _sid, _payload: None,
    ) is True
    assert session.calls == [
        "revoke",
        "process_cleanup",
        "wait_request",
        "interrupt",
    ]
    _reset_runtime()


@pytest.mark.asyncio
async def test_stop_kills_before_lock_and_rejection_then_settles_interrupted(
    isolated_db,
    monkeypatch,
) -> None:
    from host_access import processes
    from infra.database import get_db

    _reset_runtime()
    thread_id = "codex-emergency-stop"
    session = _EmergencyStopSession(thread_id)
    live = _active_live(thread_id, session, pending_tool=False)
    started = asyncio.Event()
    broadcasts = []

    async def start_input(on_write, _on_drain):
        on_write()
        started.set()
        await session.provider_exit.wait()
        raise RuntimeError("provider exited after Stop")

    async def accept_while_locked():
        async with codex_runtime._LIVE.lock(thread_id):
            return await codex_runtime._accept_app_server_input(
                thread_id, live, start_input,
                mark_accepted=None, bcast=lambda *_args: None,
            )

    class _ProcessOwner:
        async def terminate_turn(self, *_args, **_kwargs):
            session.calls.append("process_cleanup")
            return ()

    monkeypatch.setattr(processes, "PROCESS_OWNER", _ProcessOwner())
    accept_task = asyncio.create_task(accept_while_locked())
    await started.wait()
    stop_task = asyncio.create_task(codex_runtime.stop(
        thread_id, expected_turn_id=live.corsia_turn.turn_id,
        broadcast=lambda _sid, payload: broadcasts.append(payload),
    ))
    try:
        await asyncio.wait_for(
            session.kill_requested.wait(), timeout=_TEST_HANG_TIMEOUT_S,
        )
        assert codex_runtime._LIVE.lock(thread_id).locked()
        assert stop_task.done() is False
        session.provider_exit.set()
        with pytest.raises(RuntimeError, match="provider exited after Stop"):
            await accept_task
        assert await asyncio.wait_for(
            stop_task, timeout=_TEST_HANG_TIMEOUT_S,
        ) is True
        assert session.calls == [
            "provider_kill", "revoke", "process_cleanup", "drain",
        ]
        with get_db() as db:
            row = db.execute(
                "SELECT status FROM active_turns WHERE id=?",
                (live.corsia_turn.turn_id,),
            ).fetchone()
        assert row["status"] == "cancelled"
        assert [payload["type"] for payload in broadcasts] == ["turn_cancelled"]
        assert session.status_at_drain == "running"
        assert codex_runtime._LIVE.get(thread_id) is None
        successor = _admit_active_turn(
            thread_id,
            turn_id="turn-after-stop",
            content="retry",
        )
        assert successor.turn_order == live.corsia_turn.turn_order + 1
        assert persist_turn_terminal(successor.turn_id, "completed") is (
            TurnTerminalResult.APPLIED
        )
    finally:
        session.provider_exit.set()
        session.request_idle.set()
        for task in (accept_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(accept_task, stop_task, return_exceptions=True)
        _reset_runtime()


@pytest.mark.asyncio
async def test_stop_waits_for_pending_transition_then_preempts_exact_live(
    isolated_db,
    monkeypatch,
) -> None:
    from host_access import processes
    from infra.database import get_db

    _reset_runtime()
    thread_id = "codex-stop-pending-live"
    turn_id = "turn-codex-stop-pending-live"
    _insert_session(thread_id, model="codex/gpt-5-codex", spawned_by=None)
    identity = _admit_active_turn(thread_id, turn_id=turn_id)
    session = _EmergencyStopSession(thread_id)
    session.request_idle.set()
    live = codex_runtime._LiveCodex(
        codex_thread_id=session.codex_thread_id,
        session=session,
        parent_thread_id=None,
        name="codex",
        model="gpt-5-codex",
        requires_terminal_identity=True,
    )
    transition_ready = asyncio.Event()
    install_live = asyncio.Event()

    async def pending_transition() -> None:
        async with codex_runtime._LIVE.lock(thread_id):
            transition_ready.set()
            await install_live.wait()
            live.corsia_turn = identity
            codex_runtime._LIVE.put(thread_id, live)
            codex_runtime._APP_SESSIONS[thread_id] = session

    class _ProcessOwner:
        async def terminate_turn(self, *_args, **_kwargs):
            return ()

    monkeypatch.setattr(processes, "PROCESS_OWNER", _ProcessOwner())
    transition_task = asyncio.create_task(pending_transition())
    await transition_ready.wait()
    stop_task = asyncio.create_task(codex_runtime.stop(
        thread_id, expected_turn_id=turn_id,
    ))
    try:
        await asyncio.sleep(0)
        assert stop_task.done() is False
        assert session.calls == []
        install_live.set()
        await transition_task

        assert await stop_task is True
        assert session.calls == ["provider_kill", "revoke", "drain"]
        with get_db() as db:
            turn = db.execute(
                "SELECT status FROM active_turns WHERE id=?", (turn_id,),
            ).fetchone()
        assert turn["status"] == "cancelled"
        assert codex_runtime._LIVE.get(thread_id) is None
    finally:
        install_live.set()
        await asyncio.gather(transition_task, stop_task, return_exceptions=True)
        _reset_runtime()


@pytest.mark.asyncio
async def test_stop_joins_accepted_delivery_tail_before_terminal(
    isolated_db,
    monkeypatch,
) -> None:
    from host_access import processes
    from infra.database import get_db

    _reset_runtime()
    thread_id = "codex-stop-delivery-tail"
    session = _EmergencyStopSession(thread_id)
    session.request_idle.set()
    live = _active_live(thread_id, session, pending_tool=False)
    release_tail = asyncio.Event()
    tail_started = asyncio.Event()

    async def held_tail() -> None:
        tail_started.set()
        await release_tail.wait()

    class _ProcessOwner:
        async def terminate_turn(self, *_args, **_kwargs):
            return ()

    monkeypatch.setattr(processes, "PROCESS_OWNER", _ProcessOwner())
    codex_runtime._arm_delivery_event_barrier(live)
    live.delivery_event_gate.set()
    previous_tail = asyncio.create_task(held_tail())
    await tail_started.wait()
    live.delivery_event_tail = previous_tail
    statuses_at_step: list[str] = []

    def bcast(_sid: str, payload: dict) -> None:
        if payload["type"] != "step_text":
            return
        with get_db() as db:
            row = db.execute(
                "SELECT status FROM active_turns WHERE id=?",
                (live.corsia_turn.turn_id,),
            ).fetchone()
        statuses_at_step.append(row["status"])

    on_event = codex_runtime._make_on_event(live, thread_id, bcast)
    provider_task = asyncio.create_task(on_event({
        "kind": "assistant_text",
        "text": "accepted output before Stop",
        "provider_turn_id": session.active_turn_id,
    }))
    while live.delivery_event_tail is previous_tail:
        await asyncio.sleep(0)
    stop_task = asyncio.create_task(codex_runtime.stop(
        thread_id, expected_turn_id=live.corsia_turn.turn_id, broadcast=bcast,
    ))
    try:
        while "drain" not in session.calls and not stop_task.done():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert stop_task.done() is False
        release_tail.set()
        await asyncio.wait_for(provider_task, timeout=_TEST_HANG_TIMEOUT_S)
        assert await asyncio.wait_for(
            stop_task, timeout=_TEST_HANG_TIMEOUT_S,
        ) is True
        with get_db() as db:
            rows = db.execute(
                "SELECT content FROM messages WHERE thread_id=? AND role='assistant'",
                (thread_id,),
            ).fetchall()
            turn = db.execute(
                "SELECT status FROM active_turns WHERE id=?",
                (live.corsia_turn.turn_id,),
            ).fetchone()
        assert [row["content"] for row in rows] == ["accepted output before Stop"]
        assert statuses_at_step == ["running"]
        assert turn["status"] == "cancelled"
    finally:
        release_tail.set()
        await asyncio.gather(provider_task, stop_task, return_exceptions=True)
        _reset_runtime()


@pytest.mark.asyncio
async def test_stale_stop_cannot_touch_successor(isolated_db) -> None:
    _reset_runtime()
    thread_id = "codex-stop-stale-during-drain"
    old_session = _EmergencyStopSession(thread_id)
    old_live = _active_live(thread_id, old_session, pending_tool=False)
    persist_turn_terminal(old_live.corsia_turn.turn_id, "completed")
    successor_identity = _admit_active_turn(
        thread_id,
        turn_id="turn-successor",
    )
    successor_session = _EmergencyStopSession("successor")
    successor = codex_runtime._LiveCodex(
        codex_thread_id=successor_session.codex_thread_id,
        session=successor_session,
        parent_thread_id=None,
        name="codex",
        corsia_turn=successor_identity,
    )
    codex_runtime._LIVE.put(thread_id, successor)
    codex_runtime._APP_SESSIONS[thread_id] = successor_session
    codex_runtime._CODEX_THREADS[thread_id] = successor.codex_thread_id

    assert await codex_runtime.stop(
        thread_id,
        expected_turn_id=old_live.corsia_turn.turn_id,
        expected_live=old_live,
    ) is False
    assert successor_session.calls == []
    assert codex_runtime._LIVE.get(thread_id) is successor
    assert codex_runtime._APP_SESSIONS.get(thread_id) is successor_session
    _reset_runtime()


@pytest.mark.asyncio
async def test_stop_does_not_wait_for_dynamic_request_cleanup(
    isolated_db,
    monkeypatch,
) -> None:
    from host_access import processes
    from infra.database import get_db

    _reset_runtime()
    thread_id = "codex-stop-cleanup-timeout"
    session = _EmergencyStopSession(thread_id)
    live = _active_live(thread_id, session, pending_tool=False)

    class _ProcessOwner:
        async def terminate_turn(self, *_args, **_kwargs):
            return ()

    monkeypatch.setattr(processes, "PROCESS_OWNER", _ProcessOwner())
    assert await codex_runtime.stop(
        thread_id, expected_turn_id=live.corsia_turn.turn_id,
    ) is True
    with get_db() as db:
        row = db.execute(
            "SELECT status FROM active_turns WHERE id=?",
            (live.corsia_turn.turn_id,),
        ).fetchone()
    assert row["status"] == "cancelled"
    assert session.calls == ["provider_kill", "revoke", "drain"]
    assert codex_runtime._LIVE.get(thread_id) is None
    successor = _admit_active_turn(
        thread_id,
        turn_id="turn-after-cleanup-retry",
        content="retry",
    )
    assert successor.turn_order == live.corsia_turn.turn_order + 1
    _reset_runtime()


@pytest.mark.asyncio
async def test_stop_missing_durable_turn_returns_false_and_retains_owner(
    isolated_db,
    monkeypatch,
) -> None:
    from host_access import processes
    from infra.database import get_db

    _reset_runtime()
    thread_id = "codex-stop-missing-turn"
    session = _EmergencyStopSession(thread_id)
    session.request_idle.set()
    live = _active_live(thread_id, session, pending_tool=False)
    with get_db() as db:
        db.execute(
            "DELETE FROM active_turns WHERE id=?",
            (live.corsia_turn.turn_id,),
        )
        db.commit()

    class _ProcessOwner:
        async def terminate_turn(self, *_args, **_kwargs):
            return ()

    monkeypatch.setattr(processes, "PROCESS_OWNER", _ProcessOwner())

    assert await codex_runtime.stop(
        thread_id,
        expected_turn_id=live.corsia_turn.turn_id,
    ) is False
    assert session.calls == ["provider_kill", "revoke", "drain"]
    assert codex_runtime._LIVE.get(thread_id) is live
    _reset_runtime()


@pytest.mark.asyncio
async def test_stale_generation_fails_closed_before_provider_terminal(isolated_db):
    _reset_runtime()
    thread_id = "codex-stale-fence"
    session = _FenceSession(thread_id)
    live = _active_live(thread_id, session, pending_tool=False)
    task = asyncio.create_task(codex_runtime._quiesce_app_server_tool_turn(
        thread_id, live, lambda _sid, _payload: None,
    ))
    while "wait_request" not in session.calls and not task.done():
        await asyncio.sleep(0)
    assert task.done() is False
    successor = codex_runtime._LiveCodex(
        codex_thread_id=live.codex_thread_id,
        session=session,
        parent_thread_id=None,
        name="codex",
        corsia_turn=TurnIdentity("successor", 2),
    )
    codex_runtime._LIVE.put(thread_id, successor)
    session.request_idle.set()

    assert await task is False
    assert "interrupt" not in session.calls
    assert "restore" not in session.calls
    assert session.admission_open is False
    assert codex_runtime._LIVE.get(thread_id) is successor
    _reset_runtime()


@pytest.mark.asyncio
async def test_process_exit_missing_result_is_retryable_and_uses_caller_broadcast(
    isolated_db, monkeypatch,
):
    _reset_runtime()
    thread_id = "codex-missing-result-retry"
    session = _FenceSession(thread_id, request_idle=True)
    session.returncode = 7
    live = _active_live(thread_id, session)
    broadcasts: list[dict] = []
    attempts: list[dict] = []
    durable: list[dict] = []

    async def fake_run_in_db(function, *_args, **kwargs):
        name = getattr(function, "__name__", "")
        if name == "persist_tool_result":
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise RuntimeError("transient settlement failure")
            durable.append(kwargs)
            return None
        if name == "load_assistant_step_display":
            return {
                "id": "step-1",
                "content": "",
                "metadata": {"tool_calls": [{"id": "call-1"}]},
                "created_at": "2026-07-15T00:00:00+00:00",
            }
        return None

    # Deferred terminalization commits only after tool settlement succeeds.
    commit = AsyncMock(return_value=TurnTerminalResult.CONFLICTING_TERMINAL)
    monkeypatch.setattr(
        "subagents.codex_runtime_events.run_in_db", fake_run_in_db,
    )
    monkeypatch.setattr(codex_runtime, "commit_turn_terminal", commit)
    bcast = lambda _sid, payload: broadcasts.append(payload)

    with pytest.raises(RuntimeError, match="transient settlement failure"):
        await codex_runtime._finish_transport_exit(
            thread_id, live, session, 7, "dead", bcast,
        )
    assert live.tool_step_ids == {"call-1": "step-1"}
    assert live.tools_settled.is_set() is False
    assert codex_runtime._LIVE.get(thread_id) is live

    await codex_runtime._finish_transport_exit(
        thread_id, live, session, 7, "dead", bcast,
    )

    assert len(durable) == 1
    assert durable[0]["assistant_step_id"] == "step-1"
    assert durable[0]["synthetic_reason"] == "provider_missing_result"
    assert [payload["type"] for payload in broadcasts] == [
        "tool_done", "step_text",
    ]
    assert broadcasts[0]["outcome"] == "unknown"
    commit.assert_awaited_once()
    assert live.tool_step_ids == {}
    assert live.tools_settled.is_set()
    assert codex_runtime._LIVE.get(thread_id) is None
    assert thread_id not in codex_runtime._APP_SESSIONS
    _reset_runtime()


@pytest.mark.asyncio
async def test_dead_process_waits_for_buffered_real_tool_result_before_recovery(
    isolated_db, monkeypatch,
):
    """A dead process is not drained until its exact stdout reader finishes."""
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use
    from subagents.codex_app_server import CodexAppServerSession

    _reset_runtime()
    thread_id = "codex-dead-buffered-real-result"
    release_reader = asyncio.Event()

    class _Stdout:
        def __init__(self) -> None:
            self.sent_result = False

        async def readline(self) -> bytes:
            if not self.sent_result:
                await release_reader.wait()
                self.sent_result = True
                return (json.dumps({
                    "method": "item/completed",
                    "params": {
                        "threadId": f"provider-{thread_id}",
                        "turnId": "provider-turn-buffered-result",
                        "item": {
                            "id": "call-1",
                            "type": "dynamicToolCall",
                            "result": "buffered real result",
                            "success": True,
                        },
                    },
                }) + "\n").encode()
            return b""

    class _Proc:
        def __init__(self) -> None:
            self.stdout = _Stdout()
            self.returncode = 7

        async def wait(self) -> int:
            return self.returncode

    session = CodexAppServerSession(
        binary="codex",
        task=None,
        codex_thread_id=f"provider-{thread_id}",
    )
    session._track_active_turn("provider-turn-buffered-result")
    live = _active_live(thread_id, session, pending_tool=False)
    step_id = persist_tool_use(
        thread_id,
        tool_id="call-1",
        name="write_file",
        arguments={"path": "one"},
        provider="codex",
        model="gpt-5-codex",
        turn_id=live.corsia_turn.turn_id,
    )
    live.tool_names["call-1"] = "write_file"
    live.tool_step_ids["call-1"] = step_id
    live.tools_settled.clear()

    class _BufferedRegistry:
        def get(self, _name):
            return None

        async def execute(self, _name: str, _arguments: dict) -> str:
            return "buffered real result"

    from subagents.harness_tool_bridge import execute_tool

    await execute_tool(
        _BufferedRegistry(),
        thread_id=thread_id,
        turn_identity=live.corsia_turn,
        invocation_id="call-1",
        provider_tool_call_id="call-1",
        assistant_step_id=step_id,
        name="write_file",
        arguments={"path": "one"},
    )
    broadcasts: list[dict] = []
    finish_exit = AsyncMock(return_value=None)
    monkeypatch.setattr(codex_runtime, "_finish_process_exit", finish_exit)

    bcast = lambda _sid, payload: broadcasts.append(payload)
    on_event = codex_runtime._make_on_event(live, thread_id, bcast)

    async def on_exit(code: int | None, stderr: str) -> None:
        await codex_runtime._finish_transport_exit(
            thread_id, live, session, code, stderr, bcast,
        )

    session.set_callbacks(on_event=on_event, on_exit=on_exit)
    session._proc = _Proc()
    session._reader = asyncio.create_task(session._read_loop())
    quiesce = asyncio.create_task(
        codex_runtime._quiesce_app_server_tool_turn(
            thread_id, live, bcast,
        ),
    )
    try:
        while session._dynamic_tool_admission_open and not quiesce.done():
            await asyncio.sleep(0)
        assert quiesce.done() is False

        release_reader.set()
        assert await asyncio.wait_for(
            quiesce, timeout=_TEST_HANG_TIMEOUT_S,
        ) is True
        await asyncio.wait_for(
            session._reader, timeout=_TEST_HANG_TIMEOUT_S,
        )

        db = get_db()
        try:
            rows = db.execute(
                "SELECT content, metadata FROM messages "
                "WHERE thread_id=? AND role='tool'",
                (thread_id,),
            ).fetchall()
        finally:
            db.close()
        assert len(rows) == 1
        assert rows[0]["content"] == "buffered real result"
        assert json.loads(rows[0]["metadata"]).get(
            "synthetic_tool_result",
        ) is not True
        assert live.tool_step_ids == {}
        assert live.tools_settled.is_set()
        assert [event["type"] for event in broadcasts] == [
            "tool_done", "step_text",
        ]
        finish_exit.assert_awaited_once()
    finally:
        release_reader.set()
        if not quiesce.done():
            quiesce.cancel()
            with pytest.raises(asyncio.CancelledError):
                await quiesce
        if session._reader is not None and not session._reader.done():
            await session._reader
        _reset_runtime()


@pytest.mark.asyncio
async def test_done_failed_reader_allows_missing_result_recovery_retry(
    isolated_db, monkeypatch,
):
    """A stopped exceptional reader is a fence, not a permanent retry blocker."""
    from infra.database import get_db
    from subagents import codex_runtime_events
    from subagents.child_thread import persist_tool_use
    from subagents.codex_app_server import CodexAppServerSession

    _reset_runtime()
    thread_id = "codex-reader-exit-settlement-retry"

    class _Stdout:
        async def readline(self) -> bytes:
            return b""

    class _Proc:
        def __init__(self) -> None:
            self.stdout = _Stdout()
            self.returncode = 7

        async def wait(self) -> int:
            return self.returncode

    session = CodexAppServerSession(
        binary="codex",
        task=None,
        codex_thread_id=f"provider-{thread_id}",
    )
    live = _active_live(thread_id, session, pending_tool=False)
    step_id = persist_tool_use(
        thread_id,
        tool_id="call-1",
        name="write_file",
        arguments={"path": "one"},
        provider="codex",
        model="gpt-5-codex",
        turn_id=live.corsia_turn.turn_id,
    )
    live.tool_names["call-1"] = "write_file"
    live.tool_step_ids["call-1"] = step_id
    live.tools_settled.clear()
    original_run_in_db = codex_runtime_events.run_in_db
    fail_once = True

    async def flaky_run_in_db(function, *args, **kwargs):
        nonlocal fail_once
        if getattr(function, "__name__", "") == "persist_tool_result" and fail_once:
            fail_once = False
            raise RuntimeError("first exit settlement failed")
        return await original_run_in_db(function, *args, **kwargs)

    monkeypatch.setattr(codex_runtime_events, "run_in_db", flaky_run_in_db)
    commit = AsyncMock(return_value=TurnTerminalResult.CONFLICTING_TERMINAL)
    monkeypatch.setattr(codex_runtime, "commit_turn_terminal", commit)
    broadcasts: list[dict] = []
    bcast = lambda _sid, payload: broadcasts.append(payload)

    async def on_exit(code: int | None, stderr: str) -> None:
        await codex_runtime._finish_transport_exit(
            thread_id, live, session, code, stderr, bcast,
        )

    session.set_callbacks(on_exit=on_exit)
    session._proc = _Proc()
    session._reader = asyncio.create_task(session._read_loop())
    with pytest.raises(RuntimeError, match="first exit settlement failed"):
        await session._reader
    assert session._reader.done()
    assert live.tool_step_ids == {"call-1": step_id}
    assert codex_runtime._LIVE.get(thread_id) is live

    assert await codex_runtime._quiesce_app_server_tool_turn(
        thread_id, live, bcast,
    ) is True

    db = get_db()
    try:
        rows = db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND role='tool'",
            (thread_id,),
        ).fetchall()
    finally:
        db.close()
    assert len(rows) == 1
    assert json.loads(rows[0]["metadata"])["synthetic_tool_result"] is True
    assert live.tool_step_ids == {}
    assert live.tools_settled.is_set()
    assert codex_runtime._LIVE.get(thread_id) is None
    commit.assert_awaited_once()
    _reset_runtime()


@pytest.mark.asyncio
async def test_missing_results_retry_at_complete_assistant_step_boundary(
    isolated_db, monkeypatch,
):
    _reset_runtime()
    thread_id = "codex-missing-result-step-retry"
    session = _FenceSession(thread_id, request_idle=True)
    live = _active_live(thread_id, session, pending_tool=False)
    live.tool_names.update({"call-a": "write_file", "call-b": "bash"})
    live.tool_step_ids.update({"call-a": "step-a", "call-b": "step-b"})
    live.tools_settled.clear()
    attempts: list[str] = []
    broadcasts: list[dict] = []
    fail_b_once = True

    async def fake_run_in_db(function, *_args, **kwargs):
        nonlocal fail_b_once
        name = getattr(function, "__name__", "")
        if name == "persist_tool_result":
            tool_id = kwargs["tool_use_id"]
            attempts.append(tool_id)
            if tool_id == "call-b" and fail_b_once:
                fail_b_once = False
                raise RuntimeError("step-b settlement failed")
            return None
        if name == "load_assistant_step_display":
            step_id = _args[1]
            tool_id = "call-a" if step_id == "step-a" else "call-b"
            return {
                "id": step_id,
                "content": "",
                "metadata": {"tool_calls": [{"id": tool_id}]},
                "created_at": "2026-07-15T00:00:00+00:00",
            }
        return None

    monkeypatch.setattr(
        "subagents.codex_runtime_events.run_in_db", fake_run_in_db,
    )
    from subagents.codex_runtime_events import settle_missing_tool_results

    with pytest.raises(RuntimeError, match="step-b settlement failed"):
        await settle_missing_tool_results(
            live=live,
            thread_id=thread_id,
            identity=live.corsia_turn,
            bcast=lambda _sid, payload: broadcasts.append(payload),
        )

    assert live.tool_step_ids == {"call-b": "step-b"}
    assert live.tools_settled.is_set() is False
    assert [
        payload["message_id"] for payload in broadcasts
        if payload["type"] == "step_text"
    ] == ["step-a"]

    await settle_missing_tool_results(
        live=live,
        thread_id=thread_id,
        identity=live.corsia_turn,
        bcast=lambda _sid, payload: broadcasts.append(payload),
    )

    assert attempts == ["call-a", "call-b", "call-b"]
    assert [
        payload["message_id"] for payload in broadcasts
        if payload["type"] == "step_text"
    ] == ["step-a", "step-b"]
    assert live.tool_step_ids == {}
    assert live.tools_settled.is_set()
    _reset_runtime()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_event", "expected_status"),
    [
        ({
            "kind": "result",
            "result": "",
            "is_error": False,
            "stop_reason": "completed",
            "is_current_turn": True,
            "turn_id": "provider-turn",
        }, "completed"),
        ({
            "kind": "turn_failed",
            "cause": "process_exited",
            "message": "provider terminal failure",
            "is_current_turn": True,
            "turn_id": "provider-turn",
        }, "error"),
    ],
)
async def test_natural_terminal_settles_missing_tool_result_before_removing_live(
    isolated_db, monkeypatch, terminal_event, expected_status,
):
    from automation import plan_liveness
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use

    _reset_runtime()
    thread_id = f"codex-terminal-settlement-{terminal_event['kind']}"
    session = _FenceSession(thread_id, request_idle=True)
    live = _active_live(thread_id, session, pending_tool=False)
    step_id = persist_tool_use(
        thread_id,
        tool_id="call-terminal",
        name="write_file",
        arguments={"path": "missing"},
        provider="codex",
        model="gpt-5-codex",
        turn_id=live.corsia_turn.turn_id,
    )
    live.tool_names["call-terminal"] = "write_file"
    live.tool_step_ids["call-terminal"] = step_id
    live.tools_settled.clear()
    broadcasts: list[dict] = []
    monkeypatch.setattr(
        plan_liveness_terminal, "on_thread_turn_complete", lambda *_a, **_kw: None,
    )

    on_event = codex_runtime._make_on_event(
        live, thread_id,
        lambda _sid, payload: broadcasts.append(payload),
    )
    await _deliver_terminal(on_event, live, dict(terminal_event))

    db = get_db()
    try:
        tool_rows = db.execute(
            "SELECT content, metadata FROM messages "
            "WHERE thread_id=? AND role='tool'",
            (thread_id,),
        ).fetchall()
        turn_row = db.execute(
            "SELECT status FROM active_turns WHERE id=?",
            (live.corsia_turn.turn_id,),
        ).fetchone()
    finally:
        db.close()
    assert len(tool_rows) == 1
    tool_meta = json.loads(tool_rows[0]["metadata"])
    assert tool_meta["synthetic_tool_result"] is True
    assert tool_meta["synthetic_reason"] == "provider_missing_result"
    assert live.tool_step_ids == {}
    assert live.tools_settled.is_set()
    assert turn_row["status"] == expected_status
    event_types = [payload["type"] for payload in broadcasts]
    assert event_types.index("tool_done") < event_types.index("step_text")
    assert event_types.index("step_text") < event_types.index("turn_complete")
    assert codex_runtime._LIVE.get(thread_id) is None
    assert thread_id not in codex_runtime._PROVIDER_CONTEXT_UNTRUSTED
    assert codex_runtime._CODEX_THREADS[thread_id] == live.codex_thread_id
    _reset_runtime()


@pytest.mark.asyncio
async def test_terminal_settlement_failure_retains_live_and_retries(
    isolated_db, monkeypatch,
):
    from infra.database import get_db
    from subagents import codex_runtime_events
    from subagents.child_thread import persist_tool_use

    _reset_runtime()
    thread_id = "codex-terminal-settlement-retry"
    session = _FenceSession(thread_id, request_idle=True)
    live = _active_live(thread_id, session, pending_tool=False)
    step_id = persist_tool_use(
        thread_id,
        tool_id="call-retry",
        name="write_file",
        arguments={"path": "retry"},
        provider="codex",
        model="gpt-5-codex",
        turn_id=live.corsia_turn.turn_id,
    )
    live.tool_names["call-retry"] = "write_file"
    live.tool_step_ids["call-retry"] = step_id
    live.tools_settled.clear()
    original_run_in_db = codex_runtime_events.run_in_db
    fail_once = True

    async def flaky_run_in_db(function, *args, **kwargs):
        nonlocal fail_once
        if getattr(function, "__name__", "") == "persist_tool_result" and fail_once:
            fail_once = False
            raise RuntimeError("terminal settlement write failed")
        return await original_run_in_db(function, *args, **kwargs)

    monkeypatch.setattr(codex_runtime_events, "run_in_db", flaky_run_in_db)
    on_event = codex_runtime._make_on_event(
        live, thread_id, lambda _sid, _payload: None,
    )
    terminal_event = {
        "kind": "turn_failed",
        "cause": "process_exited",
        "message": "provider terminal failure",
        "is_current_turn": True,
        "turn_id": "provider-turn",
    }

    with pytest.raises(RuntimeError, match="terminal settlement write failed"):
        await _deliver_terminal(on_event, live, terminal_event)
    db = get_db()
    try:
        turn_row = db.execute(
            "SELECT status FROM active_turns WHERE id=?",
            (live.corsia_turn.turn_id,),
        ).fetchone()
    finally:
        db.close()
    assert turn_row["status"] == "running"
    assert live.tool_step_ids == {"call-retry": step_id}
    assert live.tools_settled.is_set() is False
    assert codex_runtime._LIVE.get(thread_id) is live

    await _deliver_terminal(on_event, live, terminal_event)
    assert live.tool_step_ids == {}
    assert live.tools_settled.is_set()
    assert codex_runtime._LIVE.get(thread_id) is None
    _reset_runtime()


@pytest.mark.asyncio
async def test_tool_start_persistence_failure_creates_no_settlement_debt(
    isolated_db, monkeypatch,
):
    _reset_runtime()
    thread_id = "codex-tool-start-failed"
    session = _FenceSession(thread_id, request_idle=True)
    live = _active_live(thread_id, session, pending_tool=False)

    async def fail_tool_start(function, *_args, **_kwargs):
        if getattr(function, "__name__", "") == "persist_tool_use":
            raise RuntimeError("tool start persistence failed")
        return None

    monkeypatch.setattr(
        "subagents.codex_runtime_events.run_in_db", fail_tool_start,
    )
    on_event = codex_runtime._make_on_event(
        live, thread_id, lambda _sid, _payload: None,
    )

    with pytest.raises(RuntimeError, match="tool start persistence failed"):
        await on_event({
            "kind": "tool_use", "id": "call-new", "name": "write_file",
            "input": {"path": "x"},
            "provider_thread_id": session.codex_thread_id,
            "provider_turn_id": session.active_turn_id,
        })

    assert live.tool_step_ids == {}
    assert live.tools_settled.is_set()
    _reset_runtime()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["false", "pre_write"])
async def test_fresh_rejected_start_is_unaccepted_and_not_reusable(
    isolated_db, monkeypatch, failure_mode,
):
    _reset_runtime()
    thread_id = "codex-fresh-no-provider-turn"
    turn_id = "turn-fresh-no-provider-turn"
    _insert_session(thread_id, model="codex/gpt-5-codex", spawned_by=None)
    identity = _admit_active_turn(
        thread_id,
        turn_id=turn_id,
        settle_write=False,
    )
    accepted: list[bool] = []
    created: list[object] = []
    broadcasts: list[dict] = []

    class _NoTurnSession:
        returncode = None

        def __init__(self, **_kwargs) -> None:
            self.codex_thread_id = "provider-thread-without-turn"
            self.request_log = []
            self.terminated = False
            created.append(self)

        def set_callbacks(self, **_callbacks) -> None:
            return None

        async def start(self, **_kwargs) -> bool:
            if failure_mode == "pre_write":
                raise RuntimeError("provider input rejected before write")
            return False

        async def terminate(self) -> None:
            self.terminated = True

    monkeypatch.setattr(
        "subagents.codex_app_server.CodexAppServerSession", _NoTurnSession,
    )
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.build_tool_registry", lambda _cwd: object(),
    )
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.dynamic_tool_definitions",
        lambda _registry: (),
    )

    call = locked_spawn(
        codex_runtime._spawn_app_server_turn,
        thread_id,
        "input",
        current_user_text="input",
        successor_turn_id=turn_id,
        codex_thread_id=None,
        binary="codex",
        working_dir=None,
        windows=WindowFacts(200_000, None),
        broadcast=lambda _sid, message: broadcasts.append(message),
        mark_accepted=lambda: accepted.append(True),
        preadmitted_turn=identity,
    )
    from subagents.codex_runtime_terminal import CommittedTerminalFailure

    with pytest.raises(CommittedTerminalFailure) as failure:
        await call
    assert isinstance(failure.value.publication_error, RuntimeError)
    assert failure.value.settlement is not None
    assert failure.value.settlement.result is codex_runtime.TurnTerminalResult.APPLIED
    await codex_runtime.turn_core.drive_turn_settlement(failure.value.settlement)

    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT status FROM active_turns WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
    finally:
        db.close()
    assert accepted == []
    assert row["status"] == "error"
    assert created[0].terminated is True
    assert codex_runtime._LIVE.get(thread_id) is None
    assert thread_id not in codex_runtime._APP_SESSIONS
    assert [message["type"] for message in broadcasts] == [
        "turn_activity",
        "turn_complete",
    ]
    assert {
        key: broadcasts[0][key] for key in ("turn_id", "turn_order")
    } == {
        key: broadcasts[1][key] for key in ("turn_id", "turn_order")
    }
    _reset_runtime()


@pytest.mark.asyncio
async def test_fresh_malformed_post_drain_response_is_ambiguous_and_quarantined(
    isolated_db, monkeypatch,
):
    _reset_runtime()
    thread_id = "codex-fresh-malformed-turn-response"
    turn_id = "turn-fresh-malformed-provider-id"
    _insert_session(thread_id, model="codex/gpt-5-codex", spawned_by=None)
    identity = _admit_active_turn(
        thread_id,
        turn_id=turn_id,
        settle_write=False,
    )
    delivery: list[str] = []
    created: list[object] = []

    class _MalformedTurnSession:
        returncode = None

        def __init__(self, **_kwargs) -> None:
            self.codex_thread_id = "provider-thread-ambiguous-turn"
            self.request_log = [{
                "id": 4,
                "method": "turn/start",
                "params": {"threadId": self.codex_thread_id, "input": []},
            }]
            self.terminated = False
            created.append(self)

        def set_callbacks(self, **_callbacks) -> None:
            return None

        async def start(
            self,
            *,
            on_turn_write_observed,
            on_turn_drain_confirmed,
            media_projection,
            client_message_id=None,
        ) -> bool:
            from subagents.codex_app_server_errors import CodexAppServerError

            on_turn_write_observed()
            on_turn_drain_confirmed()
            raise CodexAppServerError(
                "codex app-server did not return a turn id", {"turn": {}},
            )

        async def terminate(self) -> None:
            self.terminated = True

    monkeypatch.setattr(
        "subagents.codex_app_server.CodexAppServerSession", _MalformedTurnSession,
    )
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.build_tool_registry", lambda _cwd: object(),
    )
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.dynamic_tool_definitions",
        lambda _registry: (),
    )

    with pytest.raises(Exception, match="did not return a turn id"):
        await locked_spawn(
            codex_runtime._spawn_app_server_turn,
            thread_id,
            "input",
            current_user_text="input",
            successor_turn_id=turn_id,
            codex_thread_id=None,
            binary="codex",
            working_dir=None,
            windows=WindowFacts(200_000, None),
            broadcast=lambda *_args: None,
            mark_accepted=lambda: delivery.append("accepted"),
            mark_ambiguous=lambda: delivery.append("ambiguous"),
            preadmitted_turn=identity,
        )

    from infra.database import get_db

    live = codex_runtime._LIVE.get(thread_id)
    db = get_db()
    try:
        row = db.execute(
            "SELECT status FROM active_turns WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
    finally:
        db.close()
    assert delivery == ["ambiguous"]
    # A post-write malformed response is causally ambiguous.  It must not
    # leave the durable Turn active forever while the triggering input remains
    # fenced and provider output is quarantined.
    assert row["status"] == "error"
    assert live is not None and live.provider_context_untrusted is True
    assert [artifact["method"] for artifact in live.source_artifacts] == [
        "turn/start"
    ]
    assert codex_runtime._APP_SESSIONS.get(thread_id) is created[0]
    assert created[0].terminated is False
    _reset_runtime()


@pytest.mark.asyncio
async def test_ambiguous_bookkeeping_failure_does_not_mask_cancellation():
    async def cancelled_after_write(on_write, _on_drain):
        on_write()
        raise asyncio.CancelledError("caller cancelled")

    def fail_bookkeeping(_drained: bool) -> None:
        raise RuntimeError("tail write failed")

    with pytest.raises(asyncio.CancelledError, match="caller cancelled"):
        await codex_runtime._causal_app_server_request(
            cancelled_after_write,
            on_ambiguous=fail_bookkeeping,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_shutdown_isolates_one_live_failure_and_cleans_later_live(
    isolated_db, monkeypatch, failure,
):
    _reset_runtime()
    events: list[str] = []

    class _Session:
        def __init__(self, thread_id: str) -> None:
            self.thread_id = thread_id

        async def terminate(self) -> None:
            events.append(f"terminate:{self.thread_id}")
            if self.thread_id == "codex-first":
                if failure == "cancel":
                    raise asyncio.CancelledError()
                raise RuntimeError("first terminate failed")

    for thread_id in ("codex-first", "codex-second"):
        session = _Session(thread_id)
        _active_live(thread_id, session, pending_tool=False)
    monkeypatch.setattr(
        codex_runtime,
        "insert_message",
        lambda thread_id, *_a, **_k: events.append(f"marker:{thread_id}"),
    )

    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await codex_runtime.shutdown_all()
    else:
        with pytest.raises(RuntimeError, match="Codex shutdown incomplete"):
            await codex_runtime.shutdown_all()

    assert codex_runtime._LIVE.get("codex-first") is not None
    assert codex_runtime._LIVE.get("codex-second") is None
    assert "codex-second" not in codex_runtime._APP_SESSIONS
    assert "codex-second" not in codex_runtime._CODEX_THREADS
    assert events.index("terminate:codex-first") < events.index("terminate:codex-second")
    assert "marker:codex-second" in events
    _reset_runtime()


@pytest.mark.asyncio
async def test_shutdown_force_kills_resistant_tool_and_synthesizes_result(
    isolated_db, monkeypatch,
):
    _reset_runtime()
    thread_id = "codex-shutdown-resistant-tool"

    class _ResistantSession(_FenceSession):
        async def terminate(self) -> None:
            self.calls.append("terminate")
            self.returncode = -9
            self.request_idle.set()

    session = _ResistantSession(thread_id)
    live = _active_live(thread_id, session)
    durable: list[dict] = []

    async def fake_run_in_db(function, *_args, **kwargs):
        name = getattr(function, "__name__", "")
        if name == "persist_tool_result":
            durable.append(kwargs)
            return None
        if name == "load_assistant_step_display":
            return {
                "id": "step-1",
                "content": "",
                "metadata": {"tool_calls": [{"id": "call-1"}]},
                "created_at": "2026-07-15T00:00:00+00:00",
            }
        return None

    commit = AsyncMock(return_value=TurnTerminalResult.CONFLICTING_TERMINAL)
    monkeypatch.setattr(codex_runtime, "_DYNAMIC_TOOL_DRAIN_TIMEOUT_S", 0.01)
    monkeypatch.setattr(
        "subagents.codex_runtime_events.run_in_db", fake_run_in_db,
    )
    monkeypatch.setattr(codex_runtime, "commit_turn_terminal", commit)
    monkeypatch.setattr(codex_runtime, "insert_message", lambda *_a, **_k: None)

    await asyncio.wait_for(
        codex_runtime.shutdown_all(), timeout=_TEST_HANG_TIMEOUT_S,
    )

    assert session.calls == [
        "revoke", "wait_request", "restore", "revoke", "terminate",
        "wait_request",
    ]
    assert len(durable) == 1
    assert durable[0]["assistant_step_id"] == "step-1"
    assert durable[0]["synthetic_reason"] == "provider_missing_result"
    assert live.tools_settled.is_set()
    assert codex_runtime._LIVE.get(thread_id) is None
    assert thread_id not in codex_runtime._APP_SESSIONS
    commit.assert_awaited_once()
    _reset_runtime()


@pytest.mark.asyncio
async def test_shutdown_raises_when_successor_appears_after_live_snapshot(
    isolated_db, monkeypatch,
):
    _reset_runtime()
    thread_id = "codex-shutdown-successor-race"

    class _Session:
        returncode = None

        async def terminate(self) -> None:
            return None

    original = _Session()
    _active_live(thread_id, original, pending_tool=False)
    successor_session = _Session()
    successor: codex_runtime._LiveCodex | None = None

    def insert_successor(*_args, **_kwargs) -> None:
        nonlocal successor
        successor = codex_runtime._LiveCodex(
            codex_thread_id="provider-successor",
            session=successor_session,
            parent_thread_id=None,
            name="codex",
            model="gpt-5-codex",
        )
        codex_runtime._LIVE.put(thread_id, successor)
        codex_runtime._APP_SESSIONS[thread_id] = successor_session

    monkeypatch.setattr(codex_runtime, "insert_message", insert_successor)

    with pytest.raises(RuntimeError, match="surviving owners"):
        await codex_runtime.shutdown_all()

    assert successor is not None
    assert codex_runtime._LIVE.get(thread_id) is successor
    assert codex_runtime._APP_SESSIONS.get(thread_id) is successor_session
    _reset_runtime()


@pytest.mark.asyncio
async def test_shutdown_snapshot_never_terminates_later_successor(
    isolated_db, monkeypatch,
):
    _reset_runtime()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    calls: list[str] = []

    class _Session:
        def __init__(self, name: str, *, blocking: bool = False) -> None:
            self.name = name
            self.blocking = blocking
            self.returncode = None

        async def terminate(self) -> None:
            calls.append(f"terminate:{self.name}")
            if self.blocking:
                cleanup_started.set()
                await release_cleanup.wait()

    first_session = _Session("first", blocking=True)
    _active_live("codex-snapshot-first", first_session, pending_tool=False)
    original_second = _Session("second-original")
    _active_live("codex-snapshot-second", original_second, pending_tool=False)
    successor_session = _Session("second-successor")
    successor = codex_runtime._LiveCodex(
        codex_thread_id="provider-second-successor",
        session=successor_session,
        parent_thread_id=None,
        name="codex",
    )
    monkeypatch.setattr(codex_runtime, "insert_message", lambda *_a, **_k: None)

    shutdown = asyncio.create_task(codex_runtime.shutdown_all())
    try:
        await asyncio.wait_for(
            cleanup_started.wait(), timeout=_TEST_HANG_TIMEOUT_S,
        )
        codex_runtime._LIVE.put("codex-snapshot-second", successor)
        codex_runtime._APP_SESSIONS["codex-snapshot-second"] = successor_session
        codex_runtime._CODEX_THREADS[
            "codex-snapshot-second"
        ] = successor.codex_thread_id
        release_cleanup.set()

        with pytest.raises(RuntimeError, match="surviving owners"):
            await asyncio.wait_for(
                shutdown, timeout=_TEST_HANG_TIMEOUT_S,
            )
        assert "terminate:second-successor" not in calls
        assert codex_runtime._LIVE.get("codex-snapshot-second") is successor
        assert (
            codex_runtime._APP_SESSIONS.get("codex-snapshot-second")
            is successor_session
        )
    finally:
        release_cleanup.set()
        if not shutdown.done():
            shutdown.cancel()
            with pytest.raises(asyncio.CancelledError):
                await shutdown
        _reset_runtime()


@pytest.mark.asyncio
async def test_shutdown_snapshot_routes_naturally_finalized_live_to_idle_cleanup(
    isolated_db, monkeypatch,
):
    _reset_runtime()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    calls: list[str] = []

    class _Session:
        def __init__(self, name: str, *, blocking: bool = False) -> None:
            self.name = name
            self.blocking = blocking
            self.returncode = None

        async def terminate(self) -> None:
            calls.append(f"terminate:{self.name}")
            if self.blocking:
                cleanup_started.set()
                await release_cleanup.wait()

    first_session = _Session("first", blocking=True)
    _active_live("codex-natural-first", first_session, pending_tool=False)
    second_session = _Session("second-natural")
    second_live = _active_live(
        "codex-natural-second", second_session, pending_tool=False,
    )
    monkeypatch.setattr(codex_runtime, "insert_message", lambda *_a, **_k: None)

    shutdown = asyncio.create_task(codex_runtime.shutdown_all())
    try:
        await asyncio.wait_for(
            cleanup_started.wait(), timeout=_TEST_HANG_TIMEOUT_S,
        )
        assert second_live.corsia_turn is not None
        persist_turn_terminal(second_live.corsia_turn.turn_id, "completed")
        codex_runtime_state.finalize_live("codex-natural-second", second_live)
        release_cleanup.set()

        await asyncio.wait_for(shutdown, timeout=_TEST_HANG_TIMEOUT_S)
        assert calls.count("terminate:second-natural") == 1
        assert codex_runtime._LIVE.get("codex-natural-second") is None
        assert "codex-natural-second" not in codex_runtime._APP_SESSIONS
    finally:
        release_cleanup.set()
        if not shutdown.done():
            shutdown.cancel()
            with pytest.raises(asyncio.CancelledError):
                await shutdown
        _reset_runtime()


@pytest.mark.asyncio
async def test_same_turn_steer_does_not_revoke_dynamic_admission(isolated_db):
    _reset_runtime()
    thread_id = "codex-same-turn-steer"
    session = _FenceSession(thread_id, request_idle=True)

    async def steer(
        _text: str,
        *,
        on_write_observed,
        on_drain_confirmed,
    ) -> bool:
        session.calls.append("steer")
        on_write_observed()
        on_drain_confirmed()
        return True

    session.steer = steer
    live = _active_live(thread_id, session, pending_tool=False)
    accepted: list[bool] = []
    bind_input_turn = AsyncMock()

    assert await codex_runtime._feed_live(
        thread_id,
        "steer",
        successor_turn_id="turn-unused-successor",
        bind_input_turn=bind_input_turn,
        model="gpt-5-codex",
        mark_accepted=lambda: accepted.append(True),
    ) == live.corsia_turn
    bind_input_turn.assert_awaited_once_with(live.corsia_turn.turn_id, None)
    assert session.calls == ["steer"]
    assert accepted == [True]
    assert session.admission_open is True
    _reset_runtime()


@pytest.mark.asyncio
async def test_steer_cancel_before_write_is_unaccepted(isolated_db):
    _reset_runtime()
    thread_id = "codex-steer-cancel-before-write"
    session = _FenceSession(thread_id, request_idle=True)
    entered = asyncio.Event()

    async def steer(_text: str, **_callbacks) -> bool:
        entered.set()
        await asyncio.Event().wait()
        return True

    session.steer = steer
    live = _active_live(thread_id, session, pending_tool=False)
    accepted: list[bool] = []
    bind_input_turn = AsyncMock()
    task = asyncio.create_task(codex_runtime._feed_live(
        thread_id,
        "steer",
        successor_turn_id="turn-unused-successor",
        bind_input_turn=bind_input_turn,
        model="gpt-5-codex",
        mark_accepted=lambda: accepted.append(True),
    ))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert accepted == []
    assert live.provider_context_untrusted is False
    assert codex_runtime._LIVE.get(thread_id) is live
    _reset_runtime()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "reader_exit"])
async def test_steer_post_write_failure_is_ambiguous_and_quarantined(
    isolated_db, failure,
):
    _reset_runtime()
    thread_id = f"codex-steer-post-write-{failure}"
    session = _FenceSession(thread_id, request_idle=True)
    written = asyncio.Event()

    async def steer(
        _text: str,
        *,
        on_write_observed,
        on_drain_confirmed,
    ) -> bool:
        on_write_observed()
        written.set()
        if failure == "cancel":
            await asyncio.Event().wait()
        on_drain_confirmed()
        raise RuntimeError("reader exited after steer write")

    session.steer = steer
    live = _active_live(thread_id, session, pending_tool=False)
    accepted: list[bool] = []
    bind_input_turn = AsyncMock()
    task = asyncio.create_task(codex_runtime._feed_live(
        thread_id,
        "steer",
        successor_turn_id="turn-unused-successor",
        bind_input_turn=bind_input_turn,
        model="gpt-5-codex",
        mark_accepted=lambda: accepted.append(True),
        mark_ambiguous=lambda: accepted.append("ambiguous"),
    ))
    await written.wait()
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError, match="reader exited"):
            await task

    assert accepted == ["ambiguous"]
    assert live.provider_context_untrusted is True
    assert codex_runtime._LIVE.get(thread_id) is live
    _reset_runtime()


@pytest.mark.asyncio
async def test_ambiguous_steer_waits_for_prior_canonical_tool_result(
    isolated_db, monkeypatch,
) -> None:
    """Input B cannot terminalize A while A's admitted tool result is committing."""
    from infra.database import get_db
    from subagents.codex_app_server import CodexAppServerSession

    _reset_runtime()
    thread_id = "codex-ambiguous-steer-tool-debt"
    started = asyncio.Event()
    ambiguous = asyncio.Event()
    release = asyncio.Event()
    dynamic_request_fence_entered = asyncio.Event()
    finalized_step_started = asyncio.Event()
    release_finalized_step = asyncio.Event()

    class _PausedRegistry:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, _name):
            return None

        async def execute(self, _name: str, _arguments: dict) -> str:
            self.calls += 1
            started.set()
            await release.wait()
            return "canonical result A"

    registry = _PausedRegistry()
    _insert_session(
        thread_id,
        model="codex/gpt-5-codex",
        spawned_by=None,
    )
    identity = _admit_active_turn(
        thread_id,
        turn_id=f"turn-{thread_id}",
        content="input A",
    )
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        codex_thread_id=f"provider-{thread_id}",
        tool_registry=registry,
        corsia_thread_id=thread_id,
        corsia_turn_identity=identity,
        model="codex/gpt-5-codex",
    )
    session._track_active_turn("provider-turn-A")
    session._usage_by_turn["provider-turn-A"] = {
        "last": {"inputTokens": 100, "cachedInputTokens": 0, "outputTokens": 10},
        "total": {"inputTokens": 1000, "outputTokens": 100},
        "modelContextWindow": 100_000,
    }
    responses: list[tuple[int, dict | None, dict | None]] = []
    provider_on_event = None

    async def respond(msg_id: int, *, result=None, error=None) -> None:
        responses.append((msg_id, result, error))
        assert provider_on_event is not None
        await provider_on_event({
            "kind": "tool_result",
            "tool_use_id": "call-A",
            "content": result["contentItems"][0]["text"],
            "provider_thread_id": session.thread_id,
            "provider_turn_id": "provider-turn-A",
            "_corsia_turn_identity": identity,
        })

    async def ambiguous_steer(_text: str, **kwargs) -> bool:
        kwargs["on_write_observed"]()
        raise ConnectionError("ambiguous input B")

    session._respond = respond
    session.steer = ambiguous_steer
    original_wait_dynamic_tool_requests_idle = session.wait_dynamic_tool_requests_idle

    async def observe_dynamic_tool_fence() -> None:
        dynamic_request_fence_entered.set()
        await original_wait_dynamic_tool_requests_idle()

    monkeypatch.setattr(
        session, "wait_dynamic_tool_requests_idle", observe_dynamic_tool_fence,
    )
    live = codex_runtime._LiveCodex(
        codex_thread_id=f"provider-{thread_id}",
        session=session,
        parent_thread_id=None,
        name="codex",
        corsia_turn=identity,
        model="gpt-5-codex",
        catalog_model_id="codex/gpt-5-codex",
        context_window=100_000,
        requires_terminal_identity=True,
    )
    live.bind_app_server_session(session)
    codex_runtime._LIVE.put(thread_id, live)
    codex_runtime._APP_SESSIONS[thread_id] = session
    codex_runtime._CODEX_THREADS[thread_id] = live.codex_thread_id
    provider_on_event = codex_runtime._make_on_event(
        live,
        thread_id,
        lambda *_args: None,
    )
    original_emit_finalized_step = codex_runtime_events._emit_finalized_step

    async def pause_finalized_step(**kwargs) -> None:
        finalized_step_started.set()
        await release_finalized_step.wait()
        await original_emit_finalized_step(**kwargs)

    monkeypatch.setattr(
        codex_runtime_events, "_emit_finalized_step", pause_finalized_step,
    )
    await provider_on_event({
        "kind": "tool_use",
        "id": "call-A",
        "name": "read_file",
        "input": {"path": "A"},
        "provider_thread_id": live.codex_thread_id,
        "provider_turn_id": "provider-turn-A",
        "_corsia_turn_identity": identity,
    })

    tool_task = asyncio.create_task(session._handle_server_request({
        "id": 41,
        "method": "item/tool/call",
        "params": {
            "threadId": live.codex_thread_id,
            "turnId": "provider-turn-A",
            "callId": "call-A",
            "tool": "read_file",
            "arguments": {"path": "A"},
        },
    }))
    await asyncio.wait_for(
        started.wait(), timeout=_TEST_HANG_TIMEOUT_S,
    )
    steer_task = asyncio.create_task(codex_runtime._feed_live(
        thread_id,
        "input B",
        successor_turn_id="turn-B",
        bind_input_turn=AsyncMock(),
        mark_ambiguous=ambiguous.set,
        broadcast=lambda *_args: None,
    ))
    await asyncio.wait_for(
        ambiguous.wait(), timeout=_TEST_HANG_TIMEOUT_S,
    )
    await asyncio.wait_for(
        dynamic_request_fence_entered.wait(), timeout=_TEST_HANG_TIMEOUT_S,
    )
    db = get_db()
    try:
        pre_release = db.execute(
            "SELECT status FROM active_turns WHERE id=?",
            (identity.turn_id,),
        ).fetchone()
    finally:
        db.close()
    assert pre_release["status"] == "running"
    assert steer_task.done() is False

    release.set()
    await asyncio.wait_for(
        finalized_step_started.wait(), timeout=_TEST_HANG_TIMEOUT_S,
    )
    assert steer_task.done() is False
    release_finalized_step.set()
    await asyncio.wait_for(tool_task, timeout=_TEST_HANG_TIMEOUT_S)
    assert live.tool_step_ids == {}
    assert live.tools_settled.is_set()
    with pytest.raises(ConnectionError, match="ambiguous input B"):
        await asyncio.wait_for(steer_task, timeout=_TEST_HANG_TIMEOUT_S)
    if live.delivery_event_tail is not None:
        await asyncio.wait_for(
            live.delivery_event_tail, timeout=_TEST_HANG_TIMEOUT_S,
        )

    db = get_db()
    try:
        rows = db.execute(
            "SELECT content,metadata FROM messages "
            "WHERE thread_id=? AND role='tool'",
            (thread_id,),
        ).fetchall()
        turn = db.execute(
            "SELECT status FROM active_turns WHERE id=?",
            (identity.turn_id,),
        ).fetchone()
    finally:
        db.close()
    assert registry.calls == 1
    assert len(responses) == 1
    assert len(rows) == 1
    assert rows[0]["content"] == "canonical result A"
    assert json.loads(rows[0]["metadata"]).get("synthetic_tool_result") is not True
    assert live.tool_step_ids == {}
    assert live.tools_settled.is_set()
    assert turn["status"] == "error"
    _reset_runtime()
