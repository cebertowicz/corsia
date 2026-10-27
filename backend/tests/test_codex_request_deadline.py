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


async def test_request_deadline_includes_lock_behind_unread_pipe(monkeypatch):
    monkeypatch.setattr(codex_app_server, "STEER_REQUEST_TIMEOUT", 0.05)
    async with _unread_pipe_session() as session:
        written = asyncio.Event()
        # _notify and dynamic-tool replies share the request write lock. Their
        # stalled drain must not prevent a request's own deadline from starting.
        owner = asyncio.create_task(session._notify("initialized", {"data": "x" * (2 * 1024 * 1024)}))
        await asyncio.sleep(0)
        assert session._write_lock.locked()
        task = asyncio.create_task(session._request("turn/steer", {}, on_write_observed=written.set))
        try:
            done, _ = await asyncio.wait({task}, timeout=0.5)
            assert done, "request hung behind the transport write lock"
            with pytest.raises(CodexAppServerError, match="turn/steer"):
                task.result()
            assert not written.is_set()
            assert session._pending == session._pending_methods == {}
            await asyncio.wait_for(session._proc.wait(), 1)
            await asyncio.wait_for(session._reader, 1)
            session._on_exit.assert_awaited_once()
        finally:
            task.cancel()
            owner.cancel()
            await asyncio.gather(task, owner, return_exceptions=True)
