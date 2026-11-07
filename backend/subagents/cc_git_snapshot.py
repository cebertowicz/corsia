"""Freeze the Git snapshot setting for a registered Claude session."""

from __future__ import annotations

import asyncio
import json

from infra.db_async import run_in_db


def _store_if_missing(thread_id: str, session_id: str, value: bool) -> bool:
    from infra.database import write_transaction
    from registry import models
    from subagents.cc_invocation import claude_invocation_from_metadata, claude_invocation_metadata

    with write_transaction(None, scope="freeze_cc_git_snapshot") as db:
        row = models.find_for_thread(thread_id, "claude-code", "session", db=db)
        if not row or row["external_id"] != session_id:
            # The captured session may have been retired or replaced during capture.
            return value
        try:
            metadata = json.loads(row.get("metadata") or "{}")
            invocation = claude_invocation_from_metadata(metadata.get("claude_invocation"))
        except (TypeError, ValueError, AttributeError):
            invocation = None
        if invocation is None:
            return value
        if invocation.disable_git_snapshot is not None:
            return invocation.disable_git_snapshot
        from dataclasses import replace

        invocation = replace(invocation, disable_git_snapshot=value)
        models.update_metadata("claude-code", "session", session_id,
                               {"claude_invocation": claude_invocation_metadata(invocation)}, db=db)
        return value


async def prepare_session_git_snapshot(session, working_dir: str | None) -> None:
    """Freeze the Git setting once, away from the event loop."""
    if getattr(session, "disable_git_snapshot", None) is None:
        from subagents.managed_launch import workspace_git_snapshot_disabled

        session.disable_git_snapshot = await asyncio.to_thread(workspace_git_snapshot_disabled, working_dir)


async def prepare_startup_git_snapshot(thread_id: str, session, context) -> None:
    """Reuse a resumed session's stored setting before registering its successor."""
    if context.prior_invocation is not None and context.resume_session_id:
        session.disable_git_snapshot = await ensure_registered_git_snapshot(
            thread_id, context.resume_session_id, context.prior_invocation,
        )
    await prepare_session_git_snapshot(session, context.working_dir)


async def ensure_registered_git_snapshot(thread_id: str, session_id: str, invocation) -> bool:
    """Resolve a legacy setting off-loop and commit one value for all consumers."""
    if invocation.disable_git_snapshot is not None:
        return invocation.disable_git_snapshot
    from subagents.managed_launch import workspace_git_snapshot_disabled

    value = await asyncio.to_thread(workspace_git_snapshot_disabled, invocation.working_dir)
    return await run_in_db(_store_if_missing, thread_id, session_id, value)
