"""Request deadlines include real pipe backpressure and serialized writers."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from subagents import codex_app_server
from subagents.codex_app_server import CodexAppServerSession
from subagents.codex_app_server_errors import CodexAppServerError


@asynccontextmanager
async def _unread_pipe_session():
    # A ready, live app-server stand-in that never reads stdin. Use the actual
    # asyncio subprocess transport so write backpressure is not mocked away.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import signal; print('ready', flush=True); signal.pause()",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    session = CodexAppServerSession(binary="codex", task=None)
    session._proc = proc
    session.thread_id = "provider-thread"
    session._track_active_turn("provider-turn")
    try:
        assert await asyncio.wait_for(proc.stdout.readline(), 3) == b"ready\n"
        session.set_callbacks(on_exit=AsyncMock())
        session._reader = asyncio.create_task(session._read_loop())
        yield session
    finally:
        if proc.returncode is None:
            proc.kill()
        await asyncio.wait_for(proc.wait(), 3)
        if session._reader is not None:
            await asyncio.wait_for(session._reader, 3)
        proc.stdin.close()


@pytest.mark.parametrize("method", ["turn/steer", "turn/interrupt", "model/list", "account/rateLimits/read"])
async def test_response_deadline_preserves_session_after_drain(monkeypatch, method):
    monkeypatch.setattr(codex_app_server, "REQUEST_TIMEOUT", 0.01)
    monkeypatch.setattr(codex_app_server, "STEER_REQUEST_TIMEOUT", 0.01)
    session = CodexAppServerSession(binary="codex", task=None)
    session._proc = SimpleNamespace(
        returncode=None, stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()), kill=Mock(),
    )
    drained = Mock()
    with pytest.raises(CodexAppServerError, match=method):
        await session._request(method, {}, on_drain_confirmed=drained)
    drained.assert_called_once()
    session._proc.kill.assert_not_called()
    assert session._pending == session._pending_methods == {}
    assert not session._write_lock.locked()
    assert not session._closing


@pytest.mark.parametrize("method", ["turn/start", "thread/inject_items", "turn/interrupt", "turn/steer"])
async def test_request_deadline_includes_stopped_reader_drain(monkeypatch, method):
    monkeypatch.setattr(codex_app_server, "REQUEST_TIMEOUT", 0.05)
    monkeypatch.setattr(codex_app_server, "STEER_REQUEST_TIMEOUT", 0.05)
    async with _unread_pipe_session() as session:
        written = asyncio.Event()
        drained = asyncio.Event()
        task = asyncio.create_task(session._request(
            method, {"input": "x" * (2 * 1024 * 1024)},
            on_write_observed=written.set, on_drain_confirmed=drained.set,
        ))
        try:
            await asyncio.wait_for(written.wait(), 1)
            done, _ = await asyncio.wait({task}, timeout=0.5)
            assert done, "request hung in stdin drain before its response timer"
            with pytest.raises(CodexAppServerError, match=method):
                task.result()
            assert not drained.is_set()
            assert session._pending == session._pending_methods == {}
            assert not session._write_lock.locked()
            # Failure must leave the reader's on_exit enabled for retirement.
            assert not session._closing
            await asyncio.wait_for(session._reader, 1)
            assert session._proc.returncode is not None
            session._on_exit.assert_awaited_once()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def controlled_deadlines(monkeypatch):
    """Record real timeout scopes; tests explicitly expire them without clock races."""
    timeout = asyncio.timeout
    deadlines = []

    def controlled(budget):
        deadline = timeout(None)
        deadlines.append((budget, deadline))
        return deadline

    monkeypatch.setattr(asyncio, "timeout", controlled)
    return deadlines


async def test_request_deadline_includes_lock_behind_unread_pipe(controlled_deadlines):
    async with _unread_pipe_session() as session:
        written = Mock()
        session._kill_process = Mock(wraps=session._kill_process)
        owner = asyncio.create_task(session._notify("initialized", {"data": "x" * (2 * 1024 * 1024)}))
        task = None
        try:
            await asyncio.sleep(0)
            assert session._write_lock.locked()
            task = asyncio.create_task(session._request("turn/steer", {}, on_write_observed=written))
            await asyncio.sleep(0)
            budget, deadline = controlled_deadlines[-1]
            assert budget == 10
            deadline.reschedule(asyncio.get_running_loop().time())
            with pytest.raises(CodexAppServerError, match="turn/steer"):
                await asyncio.wait_for(asyncio.shield(task), 2)
            written.assert_not_called()
            session._kill_process.assert_not_called()
            assert not owner.done() and session._proc.returncode is None
            assert session._pending == session._pending_methods == {}
            assert [budget for budget, _ in controlled_deadlines] == [120, 10]
            controlled_deadlines[0][1].reschedule(asyncio.get_running_loop().time())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(owner), 2)
            session._kill_process.assert_called_once()
            await asyncio.wait_for(session._reader, 2)
            session._on_exit.assert_awaited_once()
            assert not session._closing
        finally:
            for pending in (task, owner):
                if pending is not None:
                    pending.cancel()
            await asyncio.gather(*(t for t in (task, owner) if t is not None), return_exceptions=True)


async def test_steer_waiter_preserves_stalled_start_own_budget(controlled_deadlines):
    async with _unread_pipe_session() as session:
        session._kill_process = Mock(wraps=session._kill_process)
        start = asyncio.create_task(session._request("turn/start", {"input": "x" * (2 * 1024 * 1024)}))
        steer = None
        try:
            await asyncio.sleep(0)
            assert session._write_lock.locked()
            steer = asyncio.create_task(session._request("turn/steer", {}))
            await asyncio.sleep(0)
            assert [budget for budget, _ in controlled_deadlines] == [120, 10]
            controlled_deadlines[1][1].reschedule(asyncio.get_running_loop().time())
            with pytest.raises(CodexAppServerError, match="turn/steer"):
                await asyncio.wait_for(asyncio.shield(steer), 2)
            session._kill_process.assert_not_called()
            assert not start.done() and session._proc.returncode is None
            assert not controlled_deadlines[0][1].expired()
            controlled_deadlines[0][1].reschedule(asyncio.get_running_loop().time())
            with pytest.raises(CodexAppServerError, match="turn/start"):
                await asyncio.wait_for(asyncio.shield(start), 2)
            session._kill_process.assert_called_once()
            await asyncio.wait_for(session._reader, 2)
            session._on_exit.assert_awaited_once()
            assert session._pending == session._pending_methods == {}
            assert not session._closing
        finally:
            for task in (start, steer):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(t for t in (start, steer) if t is not None), return_exceptions=True)


@pytest.mark.parametrize("writer", ["notify", "respond"])
async def test_nonrequest_writer_bounds_own_stalled_drain(controlled_deadlines, writer):
    async with _unread_pipe_session() as session:
        session._kill_process = Mock(wraps=session._kill_process)
        payload = {"data": "x" * (2 * 1024 * 1024)}
        operation = session._notify("initialized", payload) if writer == "notify" else session._respond(42, result=payload)
        task = asyncio.create_task(operation)
        try:
            await asyncio.sleep(0)
            assert session._write_lock.locked() and not task.done()
            assert [budget for budget, _ in controlled_deadlines] == [120]
            controlled_deadlines[0][1].reschedule(asyncio.get_running_loop().time())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 2)
            session._kill_process.assert_called_once()
            await asyncio.wait_for(session._reader, 2)
            session._on_exit.assert_awaited_once()
            assert session._proc.returncode is not None
            assert not session._closing and not session._write_lock.locked()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("writer", ["request", "notify", "respond"])
async def test_cancelled_writer_drain_does_not_kill(controlled_deadlines, writer):
    async with _unread_pipe_session() as session:
        session._kill_process = Mock(wraps=session._kill_process)
        payload = {"data": "x" * (2 * 1024 * 1024)}
        operations = {
            "request": lambda: session._request("turn/start", payload),
            "notify": lambda: session._notify("initialized", payload),
            "respond": lambda: session._respond(42, result=payload),
        }
        task = asyncio.create_task(operations[writer]())
        try:
            await asyncio.sleep(0)
            assert session._write_lock.locked() and not task.done()
            assert [budget for budget, _ in controlled_deadlines] == [120]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            session._kill_process.assert_not_called()
            assert session._proc.returncode is None
            assert not session._write_lock.locked()
            assert session._pending == session._pending_methods == {}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
