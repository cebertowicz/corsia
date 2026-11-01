"""Exact Claude Code Stop ordering across the transition lock."""

from __future__ import annotations

import asyncio

from core.stop_causes import PlanLimitStop
from core.turn_core import TerminalSettlement, drive_turn_settlement
from subagents.cc_process_exit import _CommittedTerminalFailure
from subagents.cc_runtime_helpers import _default_broadcast
from subagents.cc_runtime_quiescence import (
    cancel_admitted_tool_tasks,
    cleanup_ephemeral_live,
    halt_exact_live_before_transition,
)
from subagents.live_registry import acquire_stop_lock
from subagents.runtime_guard import join_task_through_cancellation
from subagents.cc_runtime_state import (
    Broadcast,
    _LIVE,
    _Live,
    purge_tool_token_grace,
)


async def _close_live_for_stop(
    child_thread_id: str,
    broadcast: Broadcast | None,
    *,
    expected_turn_id: str,
    stop_error: PlanLimitStop | None = None,
) -> tuple[bool, TerminalSettlement | None]:
    """Close one exact Stop owner and return work to drive after unlocking."""
    from subagents.cc_handle_close import _close_and_finalize

    live = _LIVE.get(child_thread_id)
    identity = live.corsia_turn if live is not None else None
    if identity is None or identity.turn_id != expected_turn_id:
        return False, None
    finalized, settlement = await _close_and_finalize(
        child_thread_id, live, broadcast or _default_broadcast,
        expected_identity=identity, stop_error=stop_error,
    )
    if finalized:
        purge_tool_token_grace(child_thread_id)
    return finalized, settlement


async def _acquire_stop_lock(child_thread_id: str, lock: asyncio.Lock) -> bool:
    return await acquire_stop_lock(lock, thread_id=child_thread_id, runtime="cc_runtime")


def _exact_stop_live(
    child_thread_id: str,
    expected_turn_id: str,
) -> _Live | None:
    live = _LIVE.get(child_thread_id)
    identity = live.corsia_turn if live is not None else None
    if identity is None or identity.turn_id != expected_turn_id:
        return None
    return live


async def _halt_stop_live(
    child_thread_id: str,
    expected_turn_id: str,
    live: _Live,
) -> _Live | None:
    """Latch and halt one still-current exact live generation."""
    current = _exact_stop_live(child_thread_id, expected_turn_id)
    if current is None or current is not live:
        return None
    if not live.turn_active:
        return None
    live.checkpoint_wait_scope.request_stop()
    halted = await halt_exact_live_before_transition(
        child_thread_id, expected_turn_id,
    )
    return live if halted else None


async def _halt_published_replacement(
    child_thread_id: str,
    expected_turn_id: str,
    halted_live: _Live | None,
) -> _Live | None:
    """Halt at most one distinct same-Turn generation before lock wait."""
    pending = _exact_stop_live(child_thread_id, expected_turn_id)
    if pending is None or pending is halted_live or not pending.turn_active:
        return halted_live
    return await _halt_stop_live(
        child_thread_id, expected_turn_id, pending,
    ) or halted_live


async def _close_current_stop_owner(
    child_thread_id: str,
    expected_turn_id: str,
    halted_live: _Live | None,
    broadcast: Broadcast | None,
    stop_error: PlanLimitStop | None = None,
) -> tuple[bool, TerminalSettlement | None]:
    """Re-resolve, halt, and close only the current exact Turn generation."""
    current = _exact_stop_live(child_thread_id, expected_turn_id)
    if current is None:
        return False, None
    if current is not halted_live and current.turn_active:
        halted_live = await _halt_stop_live(
            child_thread_id, expected_turn_id, current,
        )
        if halted_live is None:
            return False, None
    elif current.turn_active and halted_live is None:
        return False, None
    return await _close_live_for_stop(
        child_thread_id, broadcast, expected_turn_id=expected_turn_id,
        stop_error=stop_error,
    )


async def _stop_exact(
    child_thread_id: str,
    *,
    expected_turn_id: str | None = None,
    broadcast: Broadcast | None = None,
    cause: PlanLimitStop | None = None,
) -> bool:
    """Stop only the exact live Claude turn named by the caller."""
    if not expected_turn_id:
        return False
    lock = _LIVE.existing_lock(child_thread_id)
    registered = _LIVE.get(child_thread_id)
    identity = registered.corsia_turn if registered is not None else None
    halted_live: _Live | None = None
    if identity is None:
        # Like Codex: a held lock over no bound Turn may be starting this Turn,
        # so wait for that holder, then re-resolve under the lock.
        if lock is None or not lock.locked():
            return False
    elif identity.turn_id != expected_turn_id or not registered.turn_active:
        return False
    else:
        halted_live = await _halt_stop_live(
            child_thread_id, expected_turn_id, registered,
        )
        halted_live = await _halt_published_replacement(
            child_thread_id, expected_turn_id, halted_live,
        )
        if halted_live is None:
            return False
    publication_error: BaseException | None = None
    settlement: TerminalSettlement | None
    try:
        if lock is None:
            stopped, settlement = await _close_current_stop_owner(
                child_thread_id, expected_turn_id, halted_live, broadcast, cause,
            )
        else:
            if not await _acquire_stop_lock(child_thread_id, lock):
                return False
            try:
                stopped, settlement = await _close_current_stop_owner(
                    child_thread_id, expected_turn_id, halted_live, broadcast, cause,
                )
            finally:
                lock.release()
    except _CommittedTerminalFailure as failure:
        assert _LIVE.get(child_thread_id) is None, (
            "CC committed terminal survived exact runtime retirement"
        )
        purge_tool_token_grace(child_thread_id)
        stopped = True
        settlement = failure.settlement
        publication_error = failure.publication_error
    if settlement is not None:
        assert stopped, "CC terminal committed before exact runtime retirement"
        await drive_turn_settlement(settlement)
    if publication_error is not None:
        raise publication_error
    return stopped


def abandon_stalled_stop(
    child_thread_id: str,
    expected_turn_id: str,
    broadcast: Broadcast | None = None,
) -> object | None:
    """Detach the owner Stop could not retire by its deadline; return its process.

    The Turn is already durably terminal. Its handle leaves the registry as a
    normal Stop close would, without the stalled lock; the caller terminates
    the returned process.
    """
    from subagents.cc_handle_close import emit_logical_close

    live = _LIVE.abandon(child_thread_id, expected_turn_id)
    if live is None:
        return None
    live.checkpoint_wait_scope.request_stop()
    live.turn_active = False
    if isinstance(live.turn_terminal, asyncio.Event):
        live.turn_terminal.set()
    cancel_admitted_tool_tasks(live)
    cleanup_ephemeral_live(live, thread_id=child_thread_id)
    purge_tool_token_grace(child_thread_id)
    emit_logical_close(child_thread_id, live, broadcast or _default_broadcast)
    return live.session


async def stop(
    child_thread_id: str,
    *,
    expected_turn_id: str | None = None,
    broadcast: Broadcast | None = None,
    cause: PlanLimitStop | None = None,
) -> bool:
    """Finish one exact Stop even if its caller disconnects or is cancelled."""
    task = asyncio.create_task(_stop_exact(
        child_thread_id,
        expected_turn_id=expected_turn_id,
        broadcast=broadcast,
        cause=cause,
    ))
    cancellation = await join_task_through_cancellation(task)
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result
