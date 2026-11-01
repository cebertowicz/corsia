"""The one wake a person is owed when a Turn errored after taking in their message unanswered.

Queued inside the terminal write, driven only after the terminal lock is
released (``PendingTurnEndEffects.drives``). The message itself is never
re-written: it stays delivered in history, and this event tells the woken Turn
to answer it.
"""

from __future__ import annotations

import sqlite3
import uuid

from loguru import logger

EVENT = "system.turn.unanswered_input"
# Draft texts, awaiting the owner's approval; this is the one place they live.
SUMMARY = "The last turn ended with an error before replying, so the agent is answering again"
GUIDANCE = (
    "Your previous turn ended with an error before it answered the person's last message. "
    "Answer that message now. Verify current state first and do not repeat effects that turn "
    "already made."
)
#: Failure kinds a second Turn would meet the same way (``llm.error_cause.KINDS``).
_EXCLUDED_KINDS = ("usage_limit", "sign_in_expired", "auth_rejected", "context_overflow")


def _excluded(db: sqlite3.Connection, turn_id: str, error: str) -> bool:
    """Whether the Turn failed on its limit, sign-in/account or context size.

    The kind is the one the terminal's own error row carries (written before the
    seam by the native, Claude Code result and Codex paths); the string is read by
    the same classifiers those paths use, for an error that wrote no row.
    """
    from connections.auth_failure import auth_failure_kind
    from subagents.provider_usage_limits import is_provider_usage_or_quota_limit

    marks = ",".join("?" for _ in _EXCLUDED_KINDS)
    if db.execute(
        f"SELECT 1 FROM messages WHERE json_extract(metadata,'$.turn_id')=? "
        f"AND json_extract(metadata,'$.cause_kind') IN ({marks}) LIMIT 1",
        (turn_id, *_EXCLUDED_KINDS),
    ).fetchone() is not None:
        return True
    return bool(auth_failure_kind("claude-code", error)) or is_provider_usage_or_quota_limit(error)


def _unanswered_input(db: sqlite3.Connection, thread_id: str, turn_id: str) -> bool:
    """Whether the Turn took in a person's input that no assistant reply after it answered."""
    row = db.execute(
        "SELECT MAX(seq) AS seq FROM messages WHERE active_turn_id=? AND role='user' "
        "AND delivery_state='delivered' "
        "AND COALESCE(json_extract(metadata,'$.sender_kind'),'')<>'agent'",
        (turn_id,),
    ).fetchone()
    if row is None or row["seq"] is None:
        return False
    return db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND role='assistant' AND seq>? "
        "AND TRIM(COALESCE(content,''))<>'' AND json_extract(metadata,'$.turn_id')=? LIMIT 1",
        (thread_id, row["seq"], turn_id),
    ).fetchone() is None


def _took_in_wake(db: sqlite3.Connection, turn_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM messages WHERE active_turn_id=? AND role='system' "
        "AND json_extract(metadata,'$.event_type')=? LIMIT 1",
        (turn_id, EVENT),
    ).fetchone() is not None


def queue_unanswered_input_wake(
    db: sqlite3.Connection, thread_id: str, outcome: dict, liveness_wake, pending,
) -> None:
    """Queue one wake and mark the thread for the post-release drive, or do nothing."""
    turn_id = outcome.get("turn_id")
    if outcome.get("outcome") != "error" or not turn_id or liveness_wake is not None:
        return
    if (
        _excluded(db, turn_id, str(outcome.get("error") or ""))
        or not _unanswered_input(db, thread_id, turn_id)
        or _took_in_wake(db, turn_id)
    ):
        return
    from events.delivery import enqueue_event_wake

    try:
        enqueue_event_wake(
            thread_id, EVENT, SUMMARY, {"guidance": GUIDANCE},
            metadata={"kind": "unanswered_input", "failed_turn_id": turn_id},
            message_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"corsia-unanswered-input:{turn_id}")),
            db=db,
        )
    except ValueError as error:
        logger.warning("unanswered-input wake skipped for {}: {}", thread_id, error)
        return
    pending.drives.append(thread_id)
