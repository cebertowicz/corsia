"""Stdout reader organization for the Codex app-server session owner."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from core._tool_result_delivery_support import ToolResultBindingError
from infra.write_admission import sqlite_errorname_tag
from tools.recorded_call import CompletedToolFailure

PROCESS_EXIT_TIMEOUT_SECONDS = 10.0


async def _read_messages(session, stdout_limit: int, log: logging.Logger) -> None:
    """Read and dispatch frames until EOF, isolating only binding errors."""
    while True:
        try:
            line = await session._proc.stdout.readline()
        except (asyncio.LimitOverrunError, ValueError) as exc:
            log.error(
                "codex_app_server: dropped oversized stdout line "
                "(limit %s bytes): %s",
                stdout_limit,
                exc,
            )
            await session._emit({"kind": "stream_line_dropped"})
            continue
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log.debug("codex_app_server: skipping non-JSON line: %r", line[:120])
            continue
        try:
            await session._handle_message(msg)
        except ToolResultBindingError:
            # One unplaceable result is a transcript hole, not authority to
            # kill the session and leave all remaining calls unanswered.
            log.exception(
                "codex_app_server: tool result could not be bound on session "
                "%s; skipping this message and keeping the session alive",
                session.corsia_thread_id,
            )


def _handle_reader_crash(session, error: Exception, log: logging.Logger) -> None:
    """Latch a typed completion and terminate through the existing process."""
    if isinstance(error, CompletedToolFailure):
        session.revoke_dynamic_tool_admission()
        session.retain_completed_tool_failure(error)
    log.exception("codex_app_server: reader crashed; terminating")
    session._kill_process()


async def _close_reader_requests(session) -> None:
    """Close callbacks and fail pending requests exactly as the reader did."""
    if session._tool_requests is not None:
        await session._tool_requests.close()
    for msg_id, future in list(session._pending.items()):
        session._pending_methods.pop(msg_id, None)
        if session._pending.pop(msg_id, None) is future and not future.done():
            future.set_exception(RuntimeError("codex app-server exited before response"))


def _reader_exit_error(
    session,
    crash: BaseException | None,
    stderr: str,
) -> str | BaseException:
    """Select the typed retained result or a safe transport diagnostic."""
    if isinstance(session._terminal_error, CompletedToolFailure):
        return session._terminal_error
    if session._terminal_error is not None:
        return f"Tool usage/control failed: {type(session._terminal_error).__name__}: {session._terminal_error}"
    if crash and not stderr.strip():
        return f"CORSIA Codex app-server bridge error: {type(crash).__name__}: {crash}{sqlite_errorname_tag(crash)}"
    return stderr


async def _notify_reader_exit(session, crash: BaseException | None, log: logging.Logger) -> None:
    """Invoke the session's existing exit callback after process observation."""
    try:
        # EOF can precede the launcher's natural exit. Preserve its status,
        # then release descendants; a hung launcher still has a deadline.
        if session._proc.returncode is None:
            await asyncio.wait_for(session._proc.wait(), timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    except (OSError, RuntimeError, TimeoutError) as exc:
        crash = crash or exc
        log.warning("codex_app_server: launcher did not exit after EOF: %s", exc)
    try:
        session._kill_process()
        await session._wait_process_exit(PROCESS_EXIT_TIMEOUT_SECONDS)
    except (OSError, RuntimeError, TimeoutError) as exc:
        crash = crash or exc
        log.warning("codex_app_server: wait() after exit failed: %s", exc)
    stderr = bytes(session._stderr).decode("utf-8", "replace")
    await session._on_exit(
        session._proc.returncode,
        _reader_exit_error(session, crash, stderr),
    )


async def read_app_server(
    session: Any,
    *,
    stdout_limit: int,
    log: logging.Logger,
) -> None:
    """Run one session's reader without taking ownership from the session."""
    assert session._proc is not None and session._proc.stdout is not None
    crash: BaseException | None = None
    try:
        await _read_messages(session, stdout_limit, log)
    except Exception as exc:
        crash = exc
        _handle_reader_crash(session, exc, log)
    finally:
        await _close_reader_requests(session)
    if not session._closing and session._on_exit is not None:
        await _notify_reader_exit(session, crash, log)
