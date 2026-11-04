"""Shared identity and ownership context for Claude Code event handlers."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.turn_core import (
    TurnIdentity,
    commit_turn_terminal,
    persist_turn_settlement,
)
from infra.thread_driver import TerminalSettlement
from infra.thread_recovery import ambiguous_stale_writes
from subagents.cc_runtime_state import Broadcast, _Live

log = logging.getLogger("subagents.cc_runtime_events")

_UNSETTLED_AT_TERMINAL = "Claude Code Turn ended before its provider write settled"


@dataclass(frozen=True)
class CCEventContext:
    """One CC process handle's event-pump collaborators."""

    live: _Live
    child_id: str
    bcast: Broadcast
    db: object | None


def release_orphaned_pooled_transaction(child_id: str, kind: str) -> None:
    """Ensure an owned production event starts on a clean pooled connection."""
    from infra.database import get_db

    borrowed = get_db()
    if not borrowed.in_transaction:
        return
    log.warning(
        "cc_runtime: released an orphaned pooled transaction before %s event "
        "on %s; a prior frame wrote without committing or releasing",
        kind,
        child_id,
    )
    borrowed.close()


def event_identity(ctx: CCEventContext, ev: dict) -> TurnIdentity:
    identity = ev.get("_corsia_turn_identity")
    if not isinstance(identity, TurnIdentity):
        raise RuntimeError(
            f"CC event for {ctx.child_id} has no CORSIA turn generation"
        )
    return identity


def frame(ctx: CCEventContext, ev: dict, payload: dict) -> dict:
    return {**payload, **event_identity(ctx, ev).frame_fields()}


async def commit_cc_terminal(
    turn_id: str,
    outcome: str,
    error: str | BaseException | None = None,
    *,
    db=None,
) -> TerminalSettlement:
    """Commit one CC terminal after recording its unsettled writes ambiguous.

    As native does: the bytes may have reached the CLI, and ``finish_turn``
    refuses a Turn that still owns an unresolved provider write, which would
    leave the Turn unable to end. With ``db`` the terminal joins the caller's
    transaction and is never driven, so no idle checkpoint marker is written.
    """
    if db is not None:
        ambiguous_stale_writes(db, turn_id, error=_UNSETTLED_AT_TERMINAL)
        return persist_turn_settlement(
            turn_id, outcome, error, db=db, mark_idle_checkpoint=False,
        )
    return await commit_turn_terminal(
        turn_id, outcome, error, quarantine=_UNSETTLED_AT_TERMINAL,
    )


async def claim_terminal(
    identity: TurnIdentity,
    outcome: str,
    error: str | BaseException | None = None,
) -> TerminalSettlement:
    """Settle one result through the canonical terminal owner."""
    return await commit_cc_terminal(identity.turn_id, outcome, error)
