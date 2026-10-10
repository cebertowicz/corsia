"""Cold CC launch orchestration from one preflight-authorized replay."""

from __future__ import annotations

import asyncio
from typing import Any

from core.checkpoint_wait import CheckpointWaitScope
from core.turn_core import TurnIdentity
from infra.database import get_db
from infra.db_async import run_in_db
from llm.resolution import ExecutionTarget
from subagents.cc_startup_context import (
    build_cc_startup_context,
    record_cc_startup_context,
)
from subagents.cc_startup_process import launch_cc_process
from subagents.cc_runtime_state import _Live


def _thread_row(thread_id: str, db: Any) -> Any | None:
    return db.execute(
        "SELECT project_id,title,spawned_by,model,user_id,"
        "runtime_context_epoch FROM threads WHERE id=?",
        (thread_id,),
    ).fetchone()


def _validate_start_target(row: Any, target: ExecutionTarget) -> None:
    if target.harness != "claude-code" or not target.transport_model:
        raise ValueError("CC start received a non-Claude Code target")
    if row["model"] != target.catalog_id:
        raise ValueError("CC start target does not match the session's selected model")


def _resolve_binary(binary: str | None, db: Any) -> str:
    if binary is not None:
        return binary
    from connections.models import get_connection

    connection = get_connection("claude-code", db=db)
    return str(((connection or {}).get("config") or {}).get("binary", "claude"))


def _resolve_broadcast(row: Any, broadcast: Any) -> Any:
    if broadcast is not None:
        return broadcast
    from infra import state

    return state.owner_broadcast(row["user_id"])


async def _start_with_db(
    thread_id: str,
    *,
    target: ExecutionTarget,
    effort: str | None,
    claude_dir: str | None,
    binary: str | None,
    broadcast: Any,
    is_subagent: bool | None,
    current_user_text: str,
    prepared_context: Any,
    db: Any,
    surface_exit_error_fn: Any,
    finalize_fn: Any,
    held_lock: asyncio.Lock,
    expected_live: _Live | None,
    continuing_identity: TurnIdentity | None,
    continuing_wait_scope: CheckpointWaitScope | None,
) -> _Live | None:
    row = _thread_row(thread_id, db)
    if row is None:
        return None
    _validate_start_target(row, target)
    role = row["spawned_by"] is not None if is_subagent is None else is_subagent
    if claude_dir is None:
        from connections.account_runtime import thread_claude_dir

        claude_dir = thread_claude_dir(thread_id,
            turn_id=continuing_identity.turn_id if continuing_identity is not None else None, db=db)
    context = build_cc_startup_context(
        thread_id,
        current_user_text,
        row=row,
        target=target,
        is_subagent=role,
        prepared_context=prepared_context,
        db=db,
    )
    # The seed facts are durable writes: they run on the DB pool, not the loop.
    seed_file, artifacts = await run_in_db(
        record_cc_startup_context, thread_id, context, db=None,
    )
    return await launch_cc_process(
        thread_id,
        context,
        binary=_resolve_binary(binary, db),
        claude_dir=claude_dir,
        effort=effort,
        broadcast=_resolve_broadcast(row, broadcast),
        seed_file=seed_file,
        source_artifacts=artifacts,
        surface_exit_error_fn=surface_exit_error_fn,
        finalize_fn=finalize_fn,
        db=None,
        held_lock=held_lock,
        expected_live=expected_live,
        continuing_identity=continuing_identity,
        continuing_wait_scope=continuing_wait_scope,
    )


async def start_cc_for_row(
    thread_id: str,
    *,
    target: ExecutionTarget,
    effort: str | None = None,
    claude_dir: str | None = None,
    binary: str | None = None,
    broadcast: Any = None,
    is_subagent: bool | None = None,
    current_user_text: str | None = None,
    prepared_context: Any = None,
    db: Any = None,
    surface_exit_error_fn: Any,
    finalize_fn: Any,
    held_lock: asyncio.Lock,
    expected_live: _Live | None = None,
    continuing_identity: TurnIdentity | None = None,
    continuing_wait_scope: CheckpointWaitScope | None = None,
) -> _Live | None:
    """Start an existing CC row from the exact transcript frozen by preflight.

    The caller holds ``held_lock``, the per-thread live-handle lock. Thread
    role controls only framing and lifecycle; replay authority comes
    exclusively from the prepared destination. No artifact- or role-selected
    fallback replay exists here.
    """
    if db is not None:
        raise RuntimeError("CC async acceptance requires an owned DB connection")
    # Preparation borrows for reads; durable producers own their separate writes.
    return await _start_with_db(
        thread_id,
        target=target,
        effort=effort,
        claude_dir=claude_dir,
        binary=binary,
        broadcast=broadcast,
        is_subagent=is_subagent,
        current_user_text=current_user_text or "",
        prepared_context=prepared_context,
        db=get_db(),
        surface_exit_error_fn=surface_exit_error_fn,
        finalize_fn=finalize_fn,
        held_lock=held_lock,
        expected_live=expected_live,
        continuing_identity=continuing_identity,
        continuing_wait_scope=continuing_wait_scope,
    )
