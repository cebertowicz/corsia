"""Codex app-server response behavior at the intentional teardown boundary."""

from __future__ import annotations

import asyncio

import pytest

from subagents.codex_app_server import CodexAppServerSession


def _session_with_provider_turn() -> CodexAppServerSession:
    session = CodexAppServerSession(binary="codex", task=None)
    session.thread_id = "provider-thread"
    session._track_active_turn("provider-turn")
    return session


def _dynamic_request() -> dict:
    return {
        "id": 41,
        "method": "item/tool/call",
        "params": {
            "threadId": "provider-thread",
            "turnId": "provider-turn",
            "callId": "call-1",
            "tool": "read_file",
            "arguments": {"path": "AGENTS.md"},
        },
    }


async def test_buffered_server_request_does_not_write_after_intentional_close() -> None:
    """Removing the closing guard must reproduce the closed-stdin crash."""

    class _ClosedStdin:
        writes = 0

        def write(self, _payload: bytes) -> None:
            self.writes += 1
            raise RuntimeError("response pipe is closed")

        async def drain(self) -> None:
            raise AssertionError("a closed response pipe must not be drained")

    class _Proc:
        stdin = _ClosedStdin()
        returncode = -15

    session = _session_with_provider_turn()
    session._proc = _Proc()
    session._closing = True

    await session._handle_server_request(_dynamic_request())

    assert session._proc.stdin.writes == 0
    assert session._dynamic_tool_requests_idle.is_set()


async def test_response_pipe_failure_during_intentional_close_is_benign() -> None:
    """Teardown may close stdin while a response is awaiting its drain."""
    drain_started = asyncio.Event()
    close_transport = asyncio.Event()

    class _ClosingStdin:
        def write(self, _payload: bytes) -> None:
            return None

        async def drain(self) -> None:
            drain_started.set()
            await close_transport.wait()
            raise RuntimeError("response pipe closed during drain")

    class _Proc:
        stdin = _ClosingStdin()
        returncode = None

    session = _session_with_provider_turn()
    session._proc = _Proc()
    response = asyncio.create_task(session._respond(41, result={"ok": True}))
    await drain_started.wait()

    session._closing = True
    close_transport.set()

    await response


async def test_queued_turn_start_does_not_write_after_termination_request() -> None:
    """A Stop latched while waiting for the pipe lock must win prewrite."""
    writes: list[bytes] = []
    observed: list[str] = []

    session = CodexAppServerSession(
        binary="codex",
        task=None,
        codex_thread_id="provider-thread",
    )

    class _Stdin:
        def write(self, payload: bytes) -> None:
            writes.append(payload)

        async def drain(self) -> None:
            session._pending[1].set_result({"turn": {"id": "provider-turn"}})

    class _Proc:
        stdin = _Stdin()
        returncode = None

        def kill(self) -> None:
            return None

    session._proc = _Proc()
    await session._write_lock.acquire()
    request = asyncio.create_task(session.start_turn(
        "must-not-write-after-stop",
        on_write_observed=lambda: observed.append("observed"),
    ))
    while 1 not in session._pending:
        await asyncio.sleep(0)

    session._request_terminate()
    session._write_lock.release()

    with pytest.raises(RuntimeError, match="terminated before provider write"):
        await asyncio.wait_for(request, timeout=0.5)
    assert writes == []
    assert observed == []
    assert session.request_log == []
    assert session._pending == {}


async def test_unexpected_active_response_pipe_failure_remains_loud() -> None:
    """A dead pipe is suppressible only after CORSIA intentionally closes it."""

    class _ClosedStdin:
        def write(self, _payload: bytes) -> None:
            raise RuntimeError("unexpected response pipe closure")

        async def drain(self) -> None:
            raise AssertionError("write should fail first")

    class _Proc:
        stdin = _ClosedStdin()
        returncode = None

    session = _session_with_provider_turn()
    session._proc = _Proc()

    with pytest.raises(RuntimeError, match="unexpected response pipe closure"):
        await session._handle_server_request(_dynamic_request())
