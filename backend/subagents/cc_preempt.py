"""Interrupt or replace one exact active Claude Code turn."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal

from core.turn_core import TurnTerminalResult
from infra.queries import insert_message
from llm.resolution import ExecutionTarget
from subagents.cc_handle_close import finalize_pending_process_exit
from subagents.cc_process_exit import claim_terminal
from subagents.cc_resume_transition import replace_preempted_process
from subagents.cc_event_context_usage import (
    consume_and_attach_terminal_context_usage,
)
from subagents.cc_event_tool_settlement import (
    settle_missing_tool_results,
)
from subagents.cc_runtime_quiescence import (
    join_admitted_tool_tasks,
    prepare_live_close,
    quiesce_live_tools,
)
from subagents.cc_runtime_state import Broadcast, _CLOSE_LIVE_TIMEOUT_S, _LIVE, _Live

log = logging.getLogger("subagents.cc_runtime")

PREEMPT_TERMINAL_TIMEOUT_S = 10.0
TOOL_BOUNDARY_ACTIVE = "tool_boundary_active"


async def commit_interrupted_terminal(
    thread_id: str,
    live: _Live,
    *,
    cause: str,
    interrupt_transport: str | None = None,
    bcast: Broadcast,
    db=None,
) -> TurnTerminalResult:
    """Claim interruption before recording the hard-preempt terminal marker."""
    identity = live.corsia_turn
    if identity is None:
        return TurnTerminalResult.CONFLICTING_TERMINAL
    barrier = live.turn_terminal
    result = await claim_terminal(live, "completed", db=db, identity=identity)
    if result is not TurnTerminalResult.APPLIED:
        return result
    metadata = {
        "cc": True, "type": "result", "is_error": False,
        "stop_reason": "interrupted", "interrupted": True, "cause": cause,
        "turn_id": identity.turn_id,
    }
    if interrupt_transport is not None:
        metadata["interrupt_transport"] = interrupt_transport
    marker_id = str(uuid.uuid4())
    try:
        insert_message(thread_id, "system", "", metadata, msg_id=marker_id, db=db)
    except Exception as exc:
        log.error("CC %s interrupted marker failed: %s", thread_id, exc)
        marker_id = ""
    context_message_id = live.last_assistant_message_id or marker_id or None
    payload = {
        "type": "turn_complete", "thread_id": thread_id,
        **identity.frame_fields(),
    }
    if context_message_id is not None:
        payload["context_message_id"] = context_message_id
    if live.corsia_turn == identity:
        live.turn_active = False
        live.interrupt_requested = False
        live.apply_now_interrupt_turn = None
        if live.turn_terminal is barrier:
            live.turn_terminal = None
    try:
        await consume_and_attach_terminal_context_usage(
            live=live, child_id=thread_id, payload=payload,
            message_id=context_message_id, db=db,
        )
        bcast(thread_id, payload)
    finally:
        if barrier is not None:
            barrier.set()
    return result


async def replay_pending_exit_before_preempt(
    thread_id: str, live: _Live, bcast: Broadcast, db=None
) -> bool:
    """Settle an observed natural exit instead of interrupting it."""
    pending = live.pending_process_exit
    if pending is None:
        return False
    quiesced = await quiesce_live_tools(
        thread_id, live, pending.identity, timeout_s=_CLOSE_LIVE_TIMEOUT_S
    )
    if not quiesced:
        log.error("CC %s retained natural exit without tool quiescence", thread_id)
        return True
    if live.pending_process_exit is None:
        return True
    finalized = await finalize_pending_process_exit(
        thread_id, live, bcast, db, emit_closed=True
    )
    if not finalized:
        log.error("CC %s retained an unresolved natural process exit", thread_id)
    return True


async def _request_interrupt(
    live: _Live,
    preempt_authorized: Callable[[], Awaitable[bool]] | None,
) -> tuple[str, bool]:
    async with live.event_lock:
        if not live.turn_active:
            live.turn_terminal = None
            return "reuse", False
        if live.tool_step_ids:
            return TOOL_BOUNDARY_ACTIVE, False
        if preempt_authorized is not None and not await preempt_authorized():
            return "refused", False
        live.turn_terminal = live.turn_terminal or asyncio.Event()
        sent = await live.session.interrupt() if live.session is not None else False
        live.interrupt_requested = sent
        live.apply_now_interrupt_turn = (
            live.corsia_turn if sent and preempt_authorized is not None else None
        )
        return "wait", sent


async def _wait_for_interrupt(live: _Live, sent: bool) -> str:
    timed_out = False
    if sent:
        try:
            await asyncio.wait_for(
                live.turn_terminal.wait(), PREEMPT_TERMINAL_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            timed_out = True
    if not live.turn_active:
        terminal = live.turn_terminal
        if terminal is not None and terminal.is_set():
            live.turn_terminal = None
            live.interrupt_requested = False
            live.apply_now_interrupt_turn = None
            return "reuse"
        return "refused"
    return "timeout" if timed_out else "unavailable"


async def _authorize_hard_preempt(
    thread_id: str,
    live: _Live,
    preempt_authorized: Callable[[], Awaitable[bool]] | None,
):
    async with live.event_lock:
        live.apply_now_interrupt_turn = None
        if live.tool_step_ids:
            return None, True
        if preempt_authorized is not None and not await preempt_authorized():
            return None, False
        return live.corsia_turn, False


async def _terminate_for_preempt(
    thread_id: str, live: _Live, identity
) -> tuple[BaseException | None, bool, bool]:
    tool_boundary_active = False

    def _mark_tool_boundary() -> None:
        nonlocal tool_boundary_active
        tool_boundary_active = True

    prepared = await prepare_live_close(
        thread_id, live, identity, timeout_s=_CLOSE_LIVE_TIMEOUT_S,
        refuse_active_tools=True,
        on_active_tools=_mark_tool_boundary,
    )
    if prepared is None:
        return None, False, tool_boundary_active
    deadline, tasks = prepared
    error: BaseException | None = None
    if live.session is not None:
        try:
            await live.session.terminate()
        except BaseException as exc:
            error = exc
    quiesced = await join_admitted_tool_tasks(live, tasks, deadline=deadline)
    return error, quiesced, False


async def _settle_preempted_turn(
    thread_id: str,
    live: _Live,
    identity,
    *,
    sent: bool,
    bcast: Broadcast,
    db,
) -> bool:
    async with live.event_lock:
        if live.corsia_turn != identity or identity is None:
            log.error("CC %s hard-preempt lost its turn identity", thread_id)
            return False
        await settle_missing_tool_results(
            live=live, child_id=thread_id,
            identity=identity, bcast=bcast, db=db,
        )
        if not live.turn_active:
            return True
        result = await commit_interrupted_terminal(
            thread_id, live, cause="superseded_by_input",
            interrupt_transport=("interrupt_timeout" if sent else "interrupt_unavailable"),
            bcast=bcast, db=db,
        )
        return result in {
            TurnTerminalResult.APPLIED,
            TurnTerminalResult.ALREADY_SAME_OUTCOME,
        }


async def _hard_preempt_and_replace(
    thread_id: str,
    live: _Live,
    *,
    target: ExecutionTarget,
    effort: str | None,
    claude_dir: str | None,
    binary: str | None,
    broadcast: Broadcast | None,
    is_subagent: bool | None,
    text: str,
    bcast: Broadcast,
    held_lock: asyncio.Lock,
    prepared_context=None,
    db=None,
    refresh_prepared_context: Callable[[], Awaitable[object]] | None = None,
    preempt_authorized: Callable[[], Awaitable[bool]] | None,
    sent: bool,
) -> _Live | Literal["tool_boundary_active"] | None:
    identity, tool_boundary_active = await _authorize_hard_preempt(
        thread_id, live, preempt_authorized
    )
    if tool_boundary_active:
        return TOOL_BOUNDARY_ACTIVE
    if identity is None:
        return None
    log.warning(
        "CC %s hard-preempt: killing pid=%s to deliver mid-turn input",
        thread_id, getattr(live.session, "pid", None),
    )
    error, quiesced, tool_boundary_active = await _terminate_for_preempt(
        thread_id, live, identity
    )
    if tool_boundary_active:
        return TOOL_BOUNDARY_ACTIVE
    if not quiesced:
        if error is not None:
            raise error
        return None
    current = _LIVE.get(thread_id)
    if current is not None and current is not live:
        return None
    if current is None and live.process_exit_resolved:
        return None
    if live.pending_process_exit is not None:
        finalized = await finalize_pending_process_exit(
            thread_id, live, bcast, db, emit_closed=True
        )
        if not finalized:
            log.error("CC %s retained an unresolved natural process exit", thread_id)
            if error is not None:
                raise error
        return None
    settled = await _settle_preempted_turn(
        thread_id, live, identity, sent=sent, bcast=bcast, db=db
    )
    if error is not None:
        raise error
    if not settled:
        return None
    if refresh_prepared_context is not None:
        prepared_context = await refresh_prepared_context()
    return await replace_preempted_process(
        thread_id, target=target, effort=effort, claude_dir=claude_dir,
        binary=binary, broadcast=broadcast, is_subagent=is_subagent,
        current_user_text=text, prepared_context=prepared_context,
        bcast=bcast, db=db, held_lock=held_lock,
    )


async def preempt_active_turn(
    thread_id: str,
    live: _Live,
    *,
    target: ExecutionTarget,
    effort: str | None,
    claude_dir: str | None,
    binary: str | None,
    broadcast: Broadcast | None,
    is_subagent: bool | None,
    text: str,
    bcast: Broadcast,
    held_lock: asyncio.Lock,
    prepared_context=None,
    db=None,
    refresh_prepared_context: Callable[[], Awaitable[object]] | None = None,
    preempt_authorized: Callable[[], Awaitable[bool]] | None = None,
) -> _Live | Literal["tool_boundary_active"] | None:
    """Preempt the active turn and return an idle or rebuilt handle."""
    if await replay_pending_exit_before_preempt(thread_id, live, bcast, db):
        return None
    disposition, sent = await _request_interrupt(live, preempt_authorized)
    if disposition == TOOL_BOUNDARY_ACTIVE:
        return TOOL_BOUNDARY_ACTIVE
    if disposition == "reuse":
        return live
    if disposition == "refused":
        return None
    if await replay_pending_exit_before_preempt(thread_id, live, bcast, db):
        return None
    disposition = await _wait_for_interrupt(live, sent)
    if disposition == "reuse":
        return live
    if disposition == "refused":
        log.error("CC %s terminal post-processing remains active", thread_id)
        return None
    return await _hard_preempt_and_replace(
        thread_id, live, target=target, effort=effort,
        claude_dir=claude_dir, binary=binary, broadcast=broadcast,
        is_subagent=is_subagent, text=text, bcast=bcast, held_lock=held_lock,
        prepared_context=prepared_context, db=db,
        refresh_prepared_context=refresh_prepared_context,
        preempt_authorized=preempt_authorized, sent=sent,
    )
