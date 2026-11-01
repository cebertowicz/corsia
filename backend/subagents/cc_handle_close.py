"""Revoke, drain, terminalize, and retire exact Claude Code handles."""

from __future__ import annotations

import logging
from collections.abc import Awaitable

from core.stop_causes import PlanLimitStop
from core.turn_core import TerminalSettlement, TurnIdentity, TurnTerminalResult
from subagents.cc_process_exit import (
    _CommittedTerminalFailure, finish_process_exit, interrupt_open_turn,
)
from subagents.cc_event_result import notify_parent as _notify_parent
from subagents.cc_event_tool_settlement import settle_missing_tool_results
from subagents.cc_runtime_helpers import _default_broadcast
from subagents.cc_runtime_quiescence import (
    cleanup_ephemeral_live,
    join_admitted_tool_tasks,
    prepare_live_close,
    retire_process,
)
from subagents.cc_runtime_state import (
    Broadcast,
    _CLOSE_LIVE_TIMEOUT_S,
    _LIVE,
    _Live,
    purge_tool_token_grace,
)
from subagents.cc_stop import stop

log = logging.getLogger("subagents.cc_runtime")


def finalize(
    child_id: str,
    broadcast: Broadcast,
    db=None,
    *,
    expected_live: _Live | None = None,
    emit_closed: bool = True,
) -> _Live | None:
    """Retire one exact process handle after all admitted tool work ends."""
    live = _LIVE.get(child_id)
    if expected_live is not None and live is not expected_live:
        return None
    if live is None:
        return None
    if live.admitted_tool_tasks:
        log.error(
            "cc_runtime: %s finalization refused with %d admitted tool task(s)",
            child_id,
            len(live.admitted_tool_tasks),
        )
        return None
    live = _LIVE.pop(child_id, None)
    if live is None:
        return None
    cleanup_ephemeral_live(live, thread_id=child_id)
    if emit_closed:
        emit_logical_close(child_id, live, broadcast, db)
    return live


def emit_logical_close(
    child_id: str,
    live: _Live,
    broadcast: Broadcast,
    db=None,
) -> None:
    """Publish the logical close for a process handle retired earlier."""
    broadcast(child_id, {"type": "subagent_closed", "thread_id": child_id})
    if not live.parent_thread_id:
        return
    broadcast(live.parent_thread_id, {
        "type": "subagent_status", "thread_id": live.parent_thread_id,
        "child_thread_id": child_id, "status": "closed",
    })
    if live.user_driving:
        _notify_parent(
            live, child_id, f"Subagent “{live.name}” closed.", broadcast, db
        )


async def close_live(
    child_thread_id: str,
    broadcast: Broadcast | None,
    *,
    expected_turn_id: str,
) -> bool:
    """Close only the live handle still owning ``expected_turn_id``."""
    live = _LIVE.get(child_thread_id)
    identity = live.corsia_turn if live is not None else None
    if identity is None or identity.turn_id != expected_turn_id:
        return False
    return await close_handle(
        child_thread_id, live, broadcast, expected_identity=identity
    )


async def close_handle(
    child_thread_id: str,
    live: _Live,
    broadcast: Broadcast | None,
    *,
    expected_identity: TurnIdentity | None = None,
) -> bool:
    """Terminally close a handle and purge its final token grace."""
    closed = await close_and_finalize(
        child_thread_id, live, broadcast or _default_broadcast,
        expected_identity=expected_identity,
    )
    if closed:
        purge_tool_token_grace(child_thread_id)
    return closed


def _finalize_exact(
    child_thread_id: str,
    live: _Live,
    identity: TurnIdentity | None,
    broadcast: Broadcast,
    db,
    *,
    emit_closed: bool,
    finalize_callback,
) -> bool:
    if _LIVE.get(child_thread_id) is not live or live.corsia_turn != identity:
        return False
    return finalize_callback(
        child_thread_id, broadcast, db,
        expected_live=live, emit_closed=emit_closed,
    ) is live


async def _settle_pending_exit(
    child_thread_id: str,
    live: _Live,
    identity: TurnIdentity | None,
    broadcast: Broadcast,
    db,
    finalize_exact,
    defer_drive: bool,
) -> tuple[bool, TerminalSettlement | None]:
    pending = live.pending_process_exit
    if pending is None or pending.identity != identity:
        return False, None
    try:
        terminal = await finish_process_exit(
            child_thread_id, live, pending.code, pending.stderr, broadcast, db,
            defer_drive=defer_drive,
        )
    except _CommittedTerminalFailure:
        raise
    except BaseException:
        if live.pending_process_exit is None and live.process_exit_resolved:
            finalize_exact()
        raise
    result = terminal.result if isinstance(terminal, TerminalSettlement) else terminal
    settled = (
        result in {
            TurnTerminalResult.APPLIED,
            TurnTerminalResult.ALREADY_SAME_OUTCOME,
        }
        and live.pending_process_exit is None
    )
    settlement = (
        terminal if settled and isinstance(terminal, TerminalSettlement) else None
    )
    return settled, settlement


async def _settle_retired_turn(
    child_thread_id: str,
    live: _Live,
    identity: TurnIdentity | None,
    broadcast: Broadcast,
    db,
    *,
    emit_terminal: bool,
    defer_drive: bool,
    stop_error: PlanLimitStop | None = None,
) -> tuple[bool, TerminalSettlement | None]:
    terminal = None
    async with live.event_lock:
        if identity is not None and live.tool_step_ids:
            await settle_missing_tool_results(
                live=live, child_id=child_thread_id,
                identity=identity, bcast=broadcast, db=db,
            )
        if live.corsia_turn == identity and live.turn_active:
            terminal = await interrupt_open_turn(
                child_thread_id, live, broadcast,
                db=db, emit_terminal=emit_terminal,
                defer_drive=defer_drive, stop_error=stop_error,
            )
        settlement = terminal if isinstance(terminal, TerminalSettlement) else None
        return live.corsia_turn == identity, settlement


async def _settle_and_finalize_retired_live_for_identity(
    child_thread_id: str,
    live: _Live,
    broadcast: Broadcast,
    identity: TurnIdentity | None,
    db,
    *,
    emit_closed: bool,
    emit_terminal: bool,
    finalize_callback,
    stop_error: PlanLimitStop | None = None,
) -> tuple[bool, TerminalSettlement | None]:
    """Settle and retire a drained live only while its identity is exact.

    The terminal is claimed under ``live.event_lock`` and its successor is
    returned, never driven: a successor's dispatch re-enters this live's
    locks, so the caller drives it once they are released.
    """
    defer_drive = db is None
    current = _LIVE.get(child_thread_id)
    if current is None:
        return live.process_exit_resolved, None
    if current is not live or live.corsia_turn != identity:
        log.warning("cc_runtime: %s generation changed while draining", child_thread_id)
        return False, None
    if live.turn_active and identity is None:
        log.error("cc_runtime: active process has no identity: %s", child_thread_id)
        return False, None
    finalize_exact = lambda: _finalize_exact(
        child_thread_id, live, identity, broadcast, db,
        emit_closed=emit_closed, finalize_callback=finalize_callback,
    )
    try:
        if live.pending_process_exit is not None:
            settled, settlement = await _settle_pending_exit(
                child_thread_id, live, identity, broadcast, db, finalize_exact,
                defer_drive,
            )
        else:
            settled, settlement = await _settle_retired_turn(
                child_thread_id, live, identity, broadcast, db,
                emit_terminal=emit_terminal, defer_drive=defer_drive,
                stop_error=stop_error,
            )
    except _CommittedTerminalFailure:
        try:
            _finalize_exact(
                child_thread_id, live, identity, broadcast, db,
                emit_closed=False, finalize_callback=finalize_callback,
            )
        except BaseException:
            log.exception(
                "cc_runtime: committed terminal cleanup failed: %s", child_thread_id
            )
        assert _LIVE.get(child_thread_id) is not live, (
            "CC terminal retained exact runtime"
        )
        raise
    try:
        finalized = finalize_exact() if settled else False
    except BaseException as error:
        if settlement is None:
            raise
        assert _LIVE.get(child_thread_id) is not live, "CC terminal retained runtime"
        raise _CommittedTerminalFailure(settlement, error) from error
    return finalized, settlement


async def settle_and_finalize_retired_live_for_identity(
    child_thread_id: str,
    live: _Live,
    broadcast: Broadcast,
    identity: TurnIdentity | None,
    db,
    *,
    emit_closed: bool,
    emit_terminal: bool,
    finalize_callback,
) -> bool:
    return await _drive_after_unlock(_settle_and_finalize_retired_live_for_identity(
        child_thread_id, live, broadcast, identity, db,
        emit_closed=emit_closed, emit_terminal=emit_terminal,
        finalize_callback=finalize_callback,
    ))


async def finalize_pending_process_exit(
    child_thread_id: str,
    live: _Live,
    broadcast: Broadcast,
    db=None,
    *,
    emit_closed: bool = True,
    finalize_callback=finalize,
) -> bool:
    """Replay and retire only an already-observed exact natural exit."""
    if live.pending_process_exit is None:
        return False
    finalized = await settle_and_finalize_retired_live_for_identity(
        child_thread_id, live, broadcast, live.pending_process_exit.identity, db,
        emit_closed=emit_closed, emit_terminal=True,
        finalize_callback=finalize_callback,
    )
    return finalized


async def settle_and_finalize_retired_live(
    child_thread_id: str,
    live: _Live,
    broadcast: Broadcast,
    db=None,
    *,
    emit_closed: bool,
    emit_terminal: bool = True,
    finalize_callback=finalize,
) -> bool:
    """Settle and retire one exact process whose death is already proven."""
    return await settle_and_finalize_retired_live_for_identity(
        child_thread_id, live, broadcast, live.corsia_turn, db,
        emit_closed=emit_closed, emit_terminal=emit_terminal,
        finalize_callback=finalize_callback,
    )


async def _close_and_finalize(
    child_thread_id: str,
    live: _Live,
    broadcast: Broadcast,
    db=None,
    *,
    emit_closed: bool = True,
    finalize_callback=finalize,
    expected_identity: TurnIdentity | None = None,
    stop_error: PlanLimitStop | None = None,
) -> tuple[bool, TerminalSettlement | None]:
    """Revoke, drain, settle, terminalize, then retire the exact live.

    Returns the committed settlement undriven, as its settle step does.
    """
    identity = expected_identity or live.corsia_turn
    prepared = await prepare_live_close(
        child_thread_id, live, identity, timeout_s=_CLOSE_LIVE_TIMEOUT_S
    )
    if prepared is None:
        return False, None
    deadline, admitted_tool_tasks = prepared
    finalized, settlement = False, None
    retired, retirement_error = await retire_process(
        child_thread_id, live, deadline,
    )
    quiesced = await join_admitted_tool_tasks(
        live, admitted_tool_tasks, deadline=deadline,
    )
    if retired and not quiesced:
        log.error("cc_runtime: %s retained with active tool tasks", child_thread_id)
    if retired and quiesced:
        finalized, settlement = await _settle_and_finalize_retired_live_for_identity(
            child_thread_id, live, broadcast, identity, db,
            emit_closed=emit_closed, emit_terminal=True,
            finalize_callback=finalize_callback, stop_error=stop_error,
        )
    if retirement_error is not None:
        if settlement is not None:
            raise _CommittedTerminalFailure(
                settlement, retirement_error,
            ) from retirement_error
        raise retirement_error
    return finalized, settlement


async def close_and_finalize(
    child_thread_id: str,
    live: _Live,
    broadcast: Broadcast,
    db=None,
    *,
    emit_closed: bool = True,
    finalize_callback=finalize,
    expected_identity: TurnIdentity | None = None,
) -> bool:
    return await _drive_after_unlock(_close_and_finalize(
        child_thread_id, live, broadcast, db,
        emit_closed=emit_closed, finalize_callback=finalize_callback,
        expected_identity=expected_identity,
    ))


async def _drive_after_unlock(
    closing: Awaitable[tuple[bool, TerminalSettlement | None]],
) -> bool:
    """Drive a close's committed terminal once the live's locks are free."""
    from core.turn_core import drive_turn_settlement

    try:
        finalized, settlement = await closing
    except _CommittedTerminalFailure as failure:
        await drive_turn_settlement(failure.settlement)
        raise failure.publication_error
    if settlement is not None:
        await drive_turn_settlement(settlement)
    return finalized
