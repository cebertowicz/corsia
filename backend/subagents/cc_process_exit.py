"""Durable terminal settlement for Claude Code process exits."""

from __future__ import annotations

import logging
from typing import TypeAlias

from core.stop_causes import PlanLimitStop, frame_cause
from core.turn_core import (
    TerminalSettlement,
    TurnIdentity,
    TurnTerminalResult,
    drive_turn_settlement,
)
from infra.queries import insert_message
from subagents.cc_event_context import commit_cc_terminal
from subagents.cc_runtime_errors import surface_exit_error
from subagents.turn_account_error import claude_turn_signed_out
from subagents.cc_event_context_usage import (
    consume_and_attach_terminal_context_usage,
)
from subagents.cc_event_tool_settlement import settle_missing_tool_results
from subagents.cc_runtime_helpers import _default_broadcast
from subagents.cc_runtime_quiescence import quiesce_live_tools
from subagents.cc_runtime_state import (
    Broadcast,
    PendingProcessExit,
    _CLOSE_LIVE_TIMEOUT_S,
    _LIVE,
    _Live,
)
from subagents.completed_tool_terminal import (
    exit_detail,
    persist_completed_terminal_results,
    retained_error,
)

log = logging.getLogger("subagents.cc_runtime")

TerminalClaim: TypeAlias = TurnTerminalResult | TerminalSettlement


class _CommittedTerminalFailure(Exception):
    """Carry one committed settlement past failed publication or retirement."""

    def __init__(
        self, settlement: TerminalSettlement, publication_error: BaseException,
    ) -> None:
        super().__init__(str(publication_error))
        self.settlement = settlement
        self.publication_error = publication_error


async def claim_terminal(
    live: _Live,
    outcome: str,
    error: str | BaseException | None = None,
    *,
    db=None,
    identity: TurnIdentity | None = None,
    defer_drive: bool = False,
) -> TerminalClaim:
    identity = identity or live.corsia_turn
    if identity is None:
        return TurnTerminalResult.CONFLICTING_TERMINAL
    if defer_drive:
        assert db is None, "deferred CC terminal requires its own transaction"
    settlement = await commit_cc_terminal(identity.turn_id, outcome, error, db=db)
    if settlement.result is TurnTerminalResult.APPLIED:
        _release_owned_turn(live, identity)
    if db is not None:
        return settlement.result
    if defer_drive:
        return settlement
    return await drive_turn_settlement(settlement)


def _release_owned_turn(live: _Live, identity: TurnIdentity) -> None:
    """Release one completed CC generation before its successor is driven."""
    if live.corsia_turn != identity:
        return
    live.turn_active = False
    live.interrupt_requested = False
    live.apply_now_interrupt_turn = None


def remember_process_exit(
    live: _Live,
    identity: TurnIdentity | None,
    code: int | None,
    stderr: str | BaseException,
) -> bool:
    """Retain one exact natural exit until its durable terminal commits."""
    if identity is None or live.corsia_turn != identity or live.process_exit_resolved:
        return False
    pending = PendingProcessExit(identity, code, stderr or "")
    if live.pending_process_exit not in (None, pending):
        log.error("cc_runtime: conflicting natural exits for turn %s", identity.turn_id)
        return False
    live.pending_process_exit = pending
    return True


def _surface_process_exit(
    child_thread_id: str,
    code: int | None,
    detail: str,
    broadcast: Broadcast,
    db,
    added_signed_out: bool,
) -> str | None:
    return surface_exit_error(
        child_thread_id, code, detail, broadcast, db, None,
        insert_message_fn=insert_message, logger=log, added_signed_out=added_signed_out,
    )


async def _settle_process_exit(
    child_thread_id: str,
    live: _Live,
    identity: TurnIdentity,
    pending: PendingProcessExit,
    detail: str,
    broadcast: Broadcast,
    db,
    *,
    replaying: bool,
    defer_drive: bool = False,
) -> tuple[TerminalClaim, bool, bool]:
    async with live.event_lock:
        if live.corsia_turn != identity:
            return TurnTerminalResult.CONFLICTING_TERMINAL, False, False
        await persist_completed_terminal_results(
            identity.turn_id, pending.stderr, db=db,
        )
        await settle_missing_tool_results(
            live=live, child_id=child_thread_id,
            identity=identity, bcast=broadcast, db=db,
        )
        terminal = await claim_terminal(
            live, "error", retained_error(pending.stderr, detail), db=db, identity=identity,
            defer_drive=defer_drive,
        )
        result = terminal.result if isinstance(terminal, TerminalSettlement) else terminal
        resolved = result in {
            TurnTerminalResult.APPLIED,
            TurnTerminalResult.ALREADY_SAME_OUTCOME,
        }
        if resolved and live.pending_process_exit == pending:
            live.pending_process_exit = None
            live.process_exit_resolved = True
        complete = result is TurnTerminalResult.APPLIED or (
            result is TurnTerminalResult.ALREADY_SAME_OUTCOME and replaying
        )
        if complete and live.corsia_turn == identity:
            live.turn_active = False
            live.interrupt_requested = False
            live.apply_now_interrupt_turn = None
        return terminal, resolved, complete


async def _publish_process_exit(
    child_thread_id: str,
    live: _Live,
    identity: TurnIdentity,
    code: int | None,
    detail: str,
    broadcast: Broadcast,
    db,
) -> None:
    added_signed_out = not live.tool_boundary_marker_id and await claude_turn_signed_out(
        identity.turn_id, detail, db=db)
    marker_id = live.tool_boundary_marker_id or _surface_process_exit(
        child_thread_id, code, detail, broadcast, db, added_signed_out
    )
    payload = {
        "type": "turn_complete", "thread_id": child_thread_id,
        **identity.frame_fields(),
    }
    context_message_id = live.last_assistant_message_id or marker_id
    if context_message_id:
        payload["context_message_id"] = context_message_id
    await consume_and_attach_terminal_context_usage(
        live=live, child_id=child_thread_id, payload=payload,
        message_id=live.last_assistant_message_id, db=db,
    )
    broadcast(child_thread_id, payload)


async def finish_process_exit(
    child_thread_id: str,
    live: _Live,
    code: int | None,
    stderr: str | BaseException,
    broadcast: Broadcast,
    db=None,
    *,
    defer_drive: bool = False,
) -> TerminalClaim:
    """Settle and terminalize one exact natural process exit."""
    identity = live.corsia_turn
    replaying = live.pending_process_exit is not None
    if identity is None:
        return TurnTerminalResult.CONFLICTING_TERMINAL
    detail = exit_detail(
        stderr, code, "Claude Code", live.tool_boundary_violation_recorded,
    )
    if live.process_exit_resolved:
        async with live.event_lock:
            if live.corsia_turn != identity:
                return TurnTerminalResult.CONFLICTING_TERMINAL
            return await claim_terminal(
                live, "error", detail, db=db, identity=identity,
                defer_drive=defer_drive,
            )
    if not remember_process_exit(live, identity, code, stderr):
        return TurnTerminalResult.CONFLICTING_TERMINAL
    pending = live.pending_process_exit
    assert pending is not None
    barrier = live.turn_terminal
    terminal, resolved, complete = await _settle_process_exit(
        child_thread_id, live, identity, pending, detail, broadcast, db,
        replaying=replaying,
        defer_drive=defer_drive,
    )
    if complete and resolved:
        try:
            await _publish_process_exit(
                child_thread_id, live, identity, pending.code, detail,
                broadcast, db,
            )
        except BaseException as error:
            if defer_drive:
                assert isinstance(terminal, TerminalSettlement)
                raise _CommittedTerminalFailure(terminal, error) from error
            raise
        finally:
            if barrier is not None:
                barrier.set()
    return terminal


async def _retire_idle_exit(
    child_thread_id: str,
    live: _Live,
    broadcast: Broadcast,
    db,
    finalizer,
    emit_closed: bool,
) -> bool:
    live.process_exit_resolved = True
    if _LIVE.get(child_thread_id) is live:
        finalizer(
            child_thread_id, broadcast, db,
            expected_live=live, emit_closed=emit_closed,
        )
    retired = _LIVE.get(child_thread_id) is None
    return retired


async def _settle_active_exit(
    child_thread_id: str,
    live: _Live,
    identity: TurnIdentity | None,
    code: int | None,
    stderr: str,
    broadcast: Broadcast,
    db,
    finish_callback,
) -> bool:
    result = await finish_callback(
        child_thread_id, live, code, stderr, broadcast, db
    )
    if identity is not None and live.pending_process_exit is not None:
        raise RuntimeError(f"Claude Code process exit was not durably settled: {child_thread_id}")
    if identity is not None and result not in {
        TurnTerminalResult.APPLIED,
        TurnTerminalResult.ALREADY_SAME_OUTCOME,
    }:
        return False
    current = _LIVE.get(child_thread_id)
    return (current is live and live.corsia_turn == identity) or (
        current is None and live.process_exit_resolved
    )


async def handle_process_exit(
    child_thread_id: str,
    live: _Live,
    code: int | None,
    stderr: str,
    broadcast: Broadcast,
    db=None,
    *,
    finalize_callback,
    finish_callback=finish_process_exit,
    emit_closed: bool = True,
) -> bool:
    """Quiesce, durably finish, and retire one natural process exit."""
    identity = live.corsia_turn
    if _LIVE.get(child_thread_id) is not live:
        return False
    needs_terminal = live.pending_process_exit is not None or live.turn_active
    if needs_terminal and identity is not None and not remember_process_exit(
        live, identity, code, stderr
    ):
        return False
    quiesced = await quiesce_live_tools(
        child_thread_id, live, identity, timeout_s=_CLOSE_LIVE_TIMEOUT_S
    )
    if not quiesced:
        raise RuntimeError(f"Claude Code process exit did not quiesce: {child_thread_id}")
    if needs_terminal and identity is None:
        log.error("cc_runtime: %s process exit has no exact identity", child_thread_id)
        return False
    if not needs_terminal:
        # The callback entered while the handle looked idle. Revalidate after
        # quiescence, and never retire through an active transition fence: its
        # acceptance/Stop owner must finish or replace the bound handle.
        async with live.event_lock:
            transition_lock = _LIVE.existing_lock(child_thread_id)
            if (
                _LIVE.get(child_thread_id) is not live
                or live.corsia_turn != identity
                or live.turn_active
                or live.pending_process_exit is not None
                or (
                    identity is not None
                    and transition_lock is not None
                    and transition_lock.locked()
                )
            ):
                return False
            return await _retire_idle_exit(
                child_thread_id, live, broadcast, db,
                finalize_callback, emit_closed,
            )
    try:
        return await _settle_active_exit(child_thread_id, live, identity, code, stderr, broadcast, db, finish_callback)
    except Exception:  # nothing awaits the exit observer: this is the only trace
        log.exception("cc_runtime: %s natural exit of turn %s failed to settle", child_thread_id, identity.turn_id)
        raise
    finally:
        if live.pending_process_exit is None:
            current = _LIVE.get(child_thread_id)
            if current is live and live.corsia_turn == identity:
                finalize_callback(
                    child_thread_id, broadcast, db,
                    expected_live=live, emit_closed=emit_closed,
                )


async def interrupt_open_turn(
    child_thread_id: str,
    live: _Live,
    broadcast: Broadcast | None = None,
    *,
    db=None,
    emit_terminal: bool = True,
    defer_drive: bool = False,
    stop_error: PlanLimitStop | None = None,
) -> TerminalClaim | None:
    if not live.turn_active:
        return None
    identity = live.corsia_turn
    if identity is None:
        return TurnTerminalResult.CONFLICTING_TERMINAL
    barrier = live.turn_terminal
    terminal = await claim_terminal(
        live, "cancelled", stop_error, db=db, identity=identity,
        defer_drive=defer_drive,
    )
    result = terminal.result if isinstance(terminal, TerminalSettlement) else terminal
    if result is not TurnTerminalResult.APPLIED:
        return terminal
    payload = {
        "type": "turn_cancelled", "thread_id": child_thread_id,
        **identity.frame_fields(), **frame_cause(stop_error),
    }
    if live.last_assistant_message_id:
        payload["context_message_id"] = live.last_assistant_message_id
    if live.corsia_turn == identity:
        live.turn_active = False
        live.interrupt_requested = False
        live.apply_now_interrupt_turn = None
    try:
        await consume_and_attach_terminal_context_usage(
            live=live, child_id=child_thread_id, payload=payload,
            message_id=live.last_assistant_message_id, db=db,
        )
        if emit_terminal:
            (broadcast or _default_broadcast)(child_thread_id, payload)
    except BaseException as error:
        if defer_drive:
            assert isinstance(terminal, TerminalSettlement)
            raise _CommittedTerminalFailure(terminal, error) from error
        raise
    finally:
        if barrier is not None:
            barrier.set()
    return terminal
