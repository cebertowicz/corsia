"""The error a Claude Code result ends its Turn with, and whether a re-run answers it."""

from __future__ import annotations

from core.stop_causes import ContextOverflowEnd
from infra.db_async import run_in_db_unless_owned
from infra.plan_limit_continue import overflow_rerun_queued
from subagents.cc_event_context import CCEventContext


def turn_error(ev: dict) -> str:
    """The error a failed result records: the CLI's own code for an exhausted turn cap, else its text."""
    if ev.get("subtype") == "error_max_turns":
        return "error_max_turns"
    return ev.get("result") or "Claude Code turn failed"


def terminal_error(ev: dict, metadata: dict, outcome: str) -> str | ContextOverflowEnd | None:
    """The terminal error; a size refusal is typed so the Turn leaves its one re-run."""
    if outcome != "error":
        return None
    text = turn_error(ev)
    return ContextOverflowEnd(text) if metadata.get("cause") == "context_window_exceeded" else text


async def rerun_owns_notice(ctx: CCEventContext, turn_id: str, metadata: dict) -> bool:
    """A size refusal with its re-run queued writes no notice; the re-run answers instead."""
    if metadata.get("cause") != "context_window_exceeded":
        return False
    return await run_in_db_unless_owned(ctx.db, overflow_rerun_queued, turn_id, db=ctx.db)
