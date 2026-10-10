"""Replace and repair Claude Code provider processes from prepared replay."""

from __future__ import annotations

import asyncio
import logging

from core.checkpoint_wait import CheckpointWaitScope
from core.turn_core import TurnIdentity, drive_turn_settlement
from llm.resolution import ExecutionTarget
from subagents.cc_handle_close import emit_logical_close, finalize
from subagents.cc_process_exit import _CommittedTerminalFailure, finish_process_exit
from subagents.cc_destination_consume import build_cc_user_frame
from subagents.cc_accept import accept_reconstructed_input
from subagents.cc_runtime_helpers import _default_broadcast
from subagents.cc_runtime_quiescence import cleanup_ephemeral_live
from subagents.cc_runtime_resume import start_cc_for_row
from subagents.cc_startup_process import CCReplacementPredecessorChanged
from subagents.cc_runtime_state import (
    Broadcast,
    _LIVE,
    _Live,
    record_tool_token_rotation,
)

log = logging.getLogger("subagents.cc_runtime")


async def resume_child(
    thread_id: str,
    *,
    target: ExecutionTarget,
    effort: str | None = None,
    claude_dir: str | None = None,
    binary: str | None = None,
    broadcast: Broadcast | None = None,
    is_subagent: bool | None = None,
    current_user_text: str | None = None,
    prepared_context=None,
    db=None,
    held_lock: asyncio.Lock,
    replacement_startup: bool = False,
    expected_live: _Live | None = None,
    continuing_identity: TurnIdentity | None = None,
    continuing_wait_scope: CheckpointWaitScope | None = None,
) -> _Live | None:
    """Start from prepared replay and preserve one logical-close owner."""
    allow_logical_close = not replacement_startup

    def finalize_started_process(
        child_id: str,
        bcast: Broadcast,
        finalize_db=None,
        *,
        expected_live: _Live | None = None,
        emit_closed: bool = True,
    ) -> _Live | None:
        return finalize(
            child_id, bcast, finalize_db, expected_live=expected_live,
            emit_closed=emit_closed and allow_logical_close,
        )

    return await start_cc_for_row(
        thread_id, target=target, effort=effort, claude_dir=claude_dir,
        binary=binary, broadcast=broadcast, is_subagent=is_subagent,
        current_user_text=current_user_text, prepared_context=prepared_context,
        db=db, surface_exit_error_fn=finish_process_exit,
        finalize_fn=finalize_started_process, held_lock=held_lock,
        expected_live=expected_live,
        continuing_identity=continuing_identity,
        continuing_wait_scope=continuing_wait_scope,
    )


def _restore_unreplaced_predecessor(
    thread_id: str,
    expected_live: _Live,
    identity: TurnIdentity,
    wait_scope,
    held_lock: asyncio.Lock,
) -> None:
    """Republish the still-bound predecessor a failed replacement unregistered."""
    if (
        _LIVE.get(thread_id) is None
        and _LIVE.holds(thread_id, held_lock)
        and expected_live.turn_active
        and expected_live.corsia_turn == identity
        and expected_live.checkpoint_wait_scope is wait_scope
    ):
        _LIVE.publish(thread_id, expected_live, held=held_lock)


def _settled_replacement_boundary(thread_id: str, expected_live: _Live):
    """Resolve the exact Turn, scope, target and process a replacement takes over.

    Raises unless ``expected_live`` is still the thread's bound handle at a
    settled boundary, so the caller never starts a replacement it cannot own.
    """
    from llm.resolution import resolve_execution_target

    identity = expected_live.corsia_turn
    if identity is None:
        raise RuntimeError("Claude Code checkpoint continuation has no Turn")
    wait_scope = expected_live.checkpoint_wait_scope
    target = resolve_execution_target(expected_live.catalog_model_id or "")
    if target.harness != "claude-code" or not target.available:
        raise RuntimeError("Claude Code checkpoint target is unavailable")
    session = expected_live.session
    if session is None:
        raise RuntimeError("Claude Code checkpoint continuation has no process")
    if _LIVE.get(thread_id) is not expected_live:
        raise RuntimeError("Claude Code handle changed during checkpoint wait")
    if expected_live.corsia_turn != identity or expected_live.tool_step_ids:
        raise RuntimeError("Claude Code checkpoint boundary is not settled")
    return identity, wait_scope, target, session


async def replace_same_turn_after_checkpoint_locked(
    thread_id: str,
    expected_live: _Live,
    prepared_context,
    current_user_text: str,
    *,
    is_subagent: bool | None,
    held_lock: asyncio.Lock,
    broadcast: Broadcast | None = None,
):
    """Replace one bound CC handle without writing or ending its Turn.

    ``is_subagent`` is the role its caller already resolved; ``None`` defers to
    ``threads.spawned_by``, the single source of truth this launch and the
    prepared destination both read (issue #1617).
    """
    identity, wait_scope, target, session = _settled_replacement_boundary(
        thread_id, expected_live,
    )
    record_tool_token_rotation(thread_id, expected_live.tool_token)
    try:
        replacement = await resume_child(
            thread_id,
            target=target,
            effort=getattr(session, "effort", None),
            # The replacement launches where its rebuilt transcript is written:
            # the continuing Turn's root, not wherever the predecessor ran.
            claude_dir=None,
            binary=getattr(session, "binary", None),
            broadcast=broadcast or _default_broadcast,
            is_subagent=is_subagent,
            current_user_text=current_user_text,
            prepared_context=prepared_context,
            replacement_startup=True,
            held_lock=held_lock,
            expected_live=expected_live,
            continuing_identity=identity,
            continuing_wait_scope=wait_scope,
        )
    except CCReplacementPredecessorChanged:
        raise
    except BaseException:
        _restore_unreplaced_predecessor(
            thread_id, expected_live, identity, wait_scope, held_lock,
        )
        raise
    if replacement is None:
        _LIVE.publish(thread_id, expected_live, held=held_lock)
        raise RuntimeError("Claude Code checkpoint replacement did not start")
    if (
        replacement.corsia_turn != identity
        or replacement.checkpoint_wait_scope is not wait_scope
    ):
        raise RuntimeError("Claude Code replacement lost its exact Turn owner")
    expected_live.turn_active = False
    expected_live.corsia_turn = None
    cleanup_ephemeral_live(expected_live, thread_id=thread_id)
    await session.terminate()
    replacement.catalog_model_id = target.catalog_id
    replacement.bind_windows(target)
    replacement.runtime_context_epoch = prepared_context.runtime_context_epoch
    frame = build_cc_user_frame(
        thread_id=thread_id,
        text=current_user_text,
        live=replacement,
        target=target,
        prepared_context=prepared_context,
    )
    from core.checkpoint_cc_context import CCCheckpointContext

    replacement.session.tool_context = CCCheckpointContext(
        target, prepared_context.fit_ledger.rendered_input_tokens,
    )
    return replacement, frame, identity


async def resume_same_turn_after_checkpoint(
    thread_id: str,
    expected_live: _Live,
    prepared_context,
):
    """Swap one CC process and write the mid-Turn continuation anchor."""
    from infra.checkpoint_receipts import resume_anchor_text

    if prepared_context.replay is None:
        raise RuntimeError("Claude Code checkpoint resume is missing its prepared selection")
    anchor = resume_anchor_text(prepared_context.replay.tail.resume_presence_line)
    lock = _LIVE.lock(thread_id)
    try:
        async with lock:
            replacement, frame, identity = await replace_same_turn_after_checkpoint_locked(
                thread_id, expected_live, prepared_context, anchor,
                is_subagent=None, held_lock=lock,
            )
            return await accept_reconstructed_input(
                thread_id, replacement, frame, turn_id=identity.turn_id,
                bcast=_default_broadcast, preadmitted_turn=identity,
            )
    except _CommittedTerminalFailure as failure:
        await drive_turn_settlement(failure.settlement)
        raise failure.publication_error


async def replace_preempted_process(
    thread_id: str,
    *,
    target: ExecutionTarget,
    effort: str | None,
    claude_dir: str | None,
    binary: str | None,
    broadcast: Broadcast | None,
    is_subagent: bool | None,
    current_user_text: str,
    bcast: Broadcast,
    held_lock: asyncio.Lock,
    prepared_context=None,
    db=None,
) -> _Live | None:
    """Replace a hard-preempted process; close only if replacement fails."""
    retired = finalize(thread_id, bcast, db, emit_closed=False)
    if retired is None:
        return None
    try:
        replacement = await resume_child(
            thread_id, target=target, effort=effort, claude_dir=claude_dir,
            binary=binary, broadcast=broadcast, is_subagent=is_subagent,
            current_user_text=current_user_text, prepared_context=prepared_context,
            db=db, held_lock=held_lock, replacement_startup=True,
        )
    except BaseException:
        emit_logical_close(thread_id, retired, bcast, db)
        raise
    if replacement is None:
        emit_logical_close(thread_id, retired, bcast, db)
    return replacement


def retire_rejected_provider_session(
    thread_id: str,
    rejected_session_id: str | None,
    db,
) -> bool:
    """Retire only the exact CC session Claude rejected on resume.

    Claude rejects a session it reports missing, and one whose transcript the
    API refuses (an empty text block); either way it cannot be resumed.
    """
    if not rejected_session_id:
        return False
    from infra.database import write_transaction
    from registry import models

    with write_transaction(db, scope="retire_rejected_cc_session") as connection:
        row = models.find_for_thread(
            thread_id, "claude-code", "session", db=connection,
        )
        if row is None or row["external_id"] != rejected_session_id:
            return False
        models.retire(row["id"], db=connection)
        return True
