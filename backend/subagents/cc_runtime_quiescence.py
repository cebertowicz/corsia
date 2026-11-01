"""Exact-live Claude Code tool-authority shutdown helpers."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable

from core.turn_core import TurnIdentity
from subagents.cc_runtime_state import (
    _CLOSE_LIVE_TIMEOUT_S,
    _LIVE,
    _Live,
    record_tool_token_rotation,
)

log = logging.getLogger("subagents.cc_runtime")


def _unlink_ephemeral(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError as exc:
        log.debug("cc_runtime: ephemeral-file unlink failed: %s", exc)


def revoke_tool_access(live: _Live, *, thread_id: str | None = None) -> None:
    """Immediately revoke one CC process's loopback tool authority.

    When ``session_id`` is known, the revoked token is remembered as the
    session's immediately-previous token so the tool bridge can dual-accept
    in-flight calls for a bounded grace window and name later rejections as
    a rotation instead of a bare auth failure (issue #825).
    """
    if thread_id:
        record_tool_token_rotation(thread_id, getattr(live, "tool_token", ""))
    session = getattr(live, "session", None)
    config = getattr(live, "tool_config", "") or getattr(
        session, "mcp_config", None,
    )
    _unlink_ephemeral(config)
    live.tool_registry = None
    live.tool_token = ""
    live.tool_config = ""
    if session is not None:
        session.mcp_config = None


def cleanup_ephemeral_live(live: _Live, *, thread_id: str | None = None) -> None:
    """Delete ephemeral inputs and revoke tool access, even before tracking."""
    revoke_tool_access(live, thread_id=thread_id)
    _unlink_ephemeral(getattr(live, "seed_file", None))
    live.seed_file = ""


def cancel_admitted_tool_tasks(live: _Live) -> tuple[asyncio.Task, ...]:
    """Cancel the backend tool tasks admitted by this exact CC live."""
    tasks = tuple(live.admitted_tool_tasks)
    current = asyncio.current_task()
    for task in tasks:
        if task is not current:
            task.cancel()
    return tasks


def fence_tool_failure(
    child_thread_id: str, live: _Live, identity: TurnIdentity | None, error: Exception,
) -> bool:
    """Fence exact failed provider authority before an MCP error can escape."""
    from subagents.cc_process_exit import remember_process_exit

    if (_LIVE.get(child_thread_id) is not live or live.corsia_turn != identity
            or live.session is None or not live.turn_active):
        return False
    stopped = live.checkpoint_wait_scope.stop_requested
    pending = live.pending_process_exit
    if not stopped and pending is not None:
        from tools.recorded_call import (
            CompletedToolFailure,
            combine_completed_failures,
        )

        if pending.identity == identity and isinstance(error, CompletedToolFailure):
            from subagents.cc_runtime_state import PendingProcessExit

            live.pending_process_exit = PendingProcessExit(
                pending.identity,
                pending.code,
                combine_completed_failures(pending.stderr, error),
            )
    if not stopped and pending is None and not remember_process_exit(live, identity, 1, error):
        return False
    live.provider_context_untrusted = True
    revoke_tool_access(live, thread_id=child_thread_id)
    live.session._request_terminate()
    cancel_admitted_tool_tasks(live)
    return not stopped and pending is None


async def finish_tool_failure(
    child_thread_id: str, live: _Live, identity: TurnIdentity,
) -> None:
    """Use the retained terminal driver once the failing callback unregisters."""
    from core.turn_core import drive_turn_settlement
    from subagents.cc_handle_close import _close_and_finalize
    from subagents.cc_process_exit import _CommittedTerminalFailure
    from subagents.cc_runtime_helpers import _default_broadcast

    settlement = None
    try:
        async with _LIVE.lock(child_thread_id):
            if _LIVE.get(child_thread_id) is not live or live.corsia_turn != identity:
                return
            finalized, settlement = await _close_and_finalize(
                child_thread_id, live, _default_broadcast,
                expected_identity=identity,
            )
            if not finalized:
                raise RuntimeError(f"Claude tool failure did not retire its exact handle: {child_thread_id}")
    except _CommittedTerminalFailure as failure:
        await drive_turn_settlement(failure.settlement)
        raise failure.publication_error
    if settlement is not None:
        await drive_turn_settlement(settlement)


async def halt_exact_live_before_transition(
    child_thread_id: str,
    expected_turn_id: str,
) -> bool:
    """Kill exact provider work and cancel its scheduled replacement first."""
    live = _LIVE.get(child_thread_id)
    identity = live.corsia_turn if live is not None else None
    if identity is None or identity.turn_id != expected_turn_id:
        return False
    live.provider_context_untrusted = True
    revoke_tool_access(live, thread_id=child_thread_id)
    from host_access.processes import PROCESS_OWNER
    if live.session is not None:
        live.session._request_terminate()
    cancel_admitted_tool_tasks(live)
    try:
        await PROCESS_OWNER.terminate_turn(
            child_thread_id,
            expected_turn_id,
            foreground_only=True,
            reason="cc_turn_interrupted",
        )
    finally:
        if live.session is not None:
            await live.session.terminate()
    return True


async def join_admitted_tool_tasks(
    live: _Live,
    tasks: tuple[asyncio.Task, ...],
    *,
    deadline: float,
) -> bool:
    """Return whether the exact admitted task snapshot fully quiesced."""
    pending = {task for task in tasks if not task.done()}
    if pending:
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        _, pending = await asyncio.wait(pending, timeout=remaining)
    return not pending and not live.admitted_tool_tasks


async def quiesce_result_tools(
    child_thread_id: str, live: _Live, identity: TurnIdentity | None,
) -> bool:
    """End exact admitted waits before result settlement, preserving retained exits."""
    if live.corsia_turn != identity or not live.turn_active:
        return True
    if live.pending_process_exit is not None:
        return False
    tasks = cancel_admitted_tool_tasks(live)
    deadline = asyncio.get_running_loop().time() + _CLOSE_LIVE_TIMEOUT_S
    quiesced = await join_admitted_tool_tasks(live, tasks, deadline=deadline)
    if live.corsia_turn != identity or not live.turn_active:
        return False
    if not quiesced:
        raise RuntimeError("Claude result could not quiesce its admitted tool callbacks")
    if tasks:
        from subagents.cc_stream_control import retain_terminal_driver

        error = RuntimeError("Claude terminal ended before its tool context was measured")
        if fence_tool_failure(child_thread_id, live, identity, error):
            assert identity is not None
            # No await after scheduling: this result callback must return before
            # retained full close can join its terminal event and reader.
            retain_terminal_driver(live.session, finish_tool_failure(child_thread_id, live, identity))
        return False
    return live.pending_process_exit is None


async def retire_process(
    child_thread_id: str,
    live: _Live,
    deadline: float,
) -> tuple[bool, BaseException | None]:
    """Close one CC process, proving forced termination after close failure."""
    if live.session is None:
        return True, None
    try:
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        await asyncio.wait_for(live.session.close(), timeout=remaining)
        return True, None
    except asyncio.TimeoutError:
        log.error(
            "cc_runtime: close %s timed out after %.1fs; force-terminating",
            child_thread_id, _CLOSE_LIVE_TIMEOUT_S,
        )
        from subagents.runtime_guard import ensure_terminated
        try:
            await ensure_terminated(live.session)
        except BaseException as error:
            return False, error
        return True, None
    except BaseException as error:
        try:
            from subagents.runtime_guard import ensure_terminated

            await ensure_terminated(live.session)
        except BaseException:
            log.exception("cc_runtime: %s force-terminate failed", child_thread_id)
            return False, error
        return True, error


async def prepare_live_close(
    child_thread_id: str,
    live: _Live,
    identity: TurnIdentity | None,
    *,
    timeout_s: float,
    refuse_active_tools: bool = False,
    on_active_tools: Callable[[], None] | None = None,
) -> tuple[float, tuple[asyncio.Task, ...]] | None:
    """Quarantine an exact live and snapshot its admitted tasks."""
    async with live.event_lock:
        if live.corsia_turn != identity:
            log.warning(
                "cc_runtime: %s generation changed before process close; "
                "retaining the live guard",
                child_thread_id,
            )
            return None
        if refuse_active_tools and (
            live.tool_step_ids or not live.tools_settled.is_set()
        ):
            if on_active_tools is not None:
                on_active_tools()
            log.error(
                "cc_runtime: %s refused process close because tool ownership "
                "reappeared at the close-preparation fence",
                child_thread_id,
            )
            return None
        live.provider_context_untrusted = True
        revoke_tool_access(live, thread_id=child_thread_id)
        if live.turn_active:
            live.interrupt_requested = True
    deadline = asyncio.get_running_loop().time() + timeout_s
    return deadline, cancel_admitted_tool_tasks(live)


async def quiesce_live_tools(
    child_thread_id: str,
    live: _Live,
    identity: TurnIdentity | None,
    *,
    timeout_s: float,
) -> bool:
    """Revoke, cancel, and join exact-live tool execution within a budget."""
    prepared = await prepare_live_close(
        child_thread_id,
        live,
        identity,
        timeout_s=timeout_s,
    )
    if prepared is None:
        return False
    deadline, tasks = prepared
    return await join_admitted_tool_tasks(live, tasks, deadline=deadline)
