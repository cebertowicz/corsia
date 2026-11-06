"""The one wake a person is owed when a Turn errored after taking in their message unanswered.

Queued inside the terminal write, driven only after the terminal lock is
released (``PendingTurnEndEffects.drives``). The message itself is never
re-written: it stays delivered in history, and this event tells the woken Turn
to answer it.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from loguru import logger

from core.message_history import NON_HUMAN_MARKERS
from infra.thread_input_rows import LIVE_CLAIM

EVENT = "system.turn.unanswered_input"
# The wake's texts live only here.
SUMMARY = "The agent tried once more."
GUIDANCE = (
    "Your previous turn ended with an error before it answered the person's last message. "
    "Answer that message now. Verify current state first and do not repeat effects that turn "
    "already made."
)
#: Failure kinds a second Turn would meet the same way (``llm.error_cause.KINDS``).
_EXCLUDED_KINDS = ("usage_limit", "sign_in_expired", "auth_rejected", "context_overflow")


def _excluded(db: sqlite3.Connection, thread_id: str, turn_id: str, error: str) -> bool:
    """Whether the Turn failed on its limit, sign-in/account or context size.

    The kind is the one the terminal's own error row carries (written before the
    seam by the native, Claude Code result and Codex paths); the string is read by
    the same classifiers those paths use, for an error that wrote no row. The
    string's auth check knows only Claude Code's text, so a native or Codex auth
    error with no row is not excluded.
    """
    from connections.auth_failure import auth_failure_kind
    from subagents.provider_usage_limits import is_provider_usage_or_quota_limit

    marks = ",".join("?" for _ in _EXCLUDED_KINDS)
    if db.execute(
        f"SELECT 1 FROM messages WHERE thread_id=? AND role='system' "
        f"AND json_extract(metadata,'$.turn_id')=? "
        f"AND json_extract(metadata,'$.cause_kind') IN ({marks}) LIMIT 1",
        (thread_id, turn_id, *_EXCLUDED_KINDS),
    ).fetchone() is not None:
        return True
    return ("error_max_turns" in error or bool(auth_failure_kind("claude-code", error))
            or is_provider_usage_or_quota_limit(error))


def _is_person(metadata: str | None) -> bool:
    return not any(json.loads(metadata or "{}").get(key) for key in NON_HUMAN_MARKERS)


def _unanswered_input(db: sqlite3.Connection, thread_id: str, turn_id: str) -> bool:
    """Whether the Turn took in a person's input that no assistant reply after it answered."""
    rows = db.execute(
        "SELECT seq, metadata FROM messages WHERE active_turn_id=? AND role='user' "
        "AND delivery_state='delivered' AND seq IS NOT NULL",
        (turn_id,),
    ).fetchall()
    seqs = [row["seq"] for row in rows if _is_person(row["metadata"])]
    if not seqs:
        return False
    return db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND role='assistant' AND seq>? "
        "AND TRIM(COALESCE(content,''))<>'' AND json_extract(metadata,'$.turn_id')=? LIMIT 1",
        (thread_id, max(seqs), turn_id),
    ).fetchone() is None


def _person_input_queued(db: sqlite3.Connection, thread_id: str) -> bool:
    """Whether a person's queued input will start a Turn that sees the message."""
    return any(_is_person(row["metadata"]) for row in db.execute(
        "SELECT m.metadata FROM messages m WHERE m.thread_id=? AND m.role='user' "
        "AND m.delivery_state='queued' AND m.input_order IS NOT NULL "
        f"AND {LIVE_CLAIM}",
        (thread_id,),
    ))


def _took_in_wake(db: sqlite3.Connection, turn_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM messages WHERE active_turn_id=? AND role='system' "
        "AND json_extract(metadata,'$.event_type')=? LIMIT 1",
        (turn_id, EVENT),
    ).fetchone() is not None


def _restart_resumes(db: sqlite3.Connection, thread_id: str, turn_id: str) -> bool:
    """Whether a restart registration already owns resuming this Turn (``automation.restart_wakes``)."""
    return db.execute(
        "SELECT 1 FROM restart_wake_registrations WHERE thread_id=? AND turn_id=? LIMIT 1",
        (thread_id, turn_id),
    ).fetchone() is not None


def queue_unanswered_input_wake(
    db: sqlite3.Connection, thread_id: str, outcome: dict, liveness_wake, pending,
) -> None:
    """Queue one wake and mark the thread for the post-release drive, or do nothing.

    A provider-reset liveness wake replaces it (the provider can't serve the
    thread yet), and so does a restart registration for the Turn, whose own wake
    resumes it; a plan backoff wake does not, since it neither fires soon nor
    names the person's message. A person's queued input replaces it too: its
    Turn answers with the message in history, and recovery may already have
    claimed it, so the wake would be a second Turn after it. If that Turn fails
    before writing, the input shows failed with Retry and is not re-woken here.
    """
    turn_id = outcome.get("turn_id")
    if outcome.get("outcome") != "error" or not turn_id:
        return
    if liveness_wake is not None and liveness_wake.provider_reset_at is not None:
        return
    if (
        not _unanswered_input(db, thread_id, turn_id)
        or _excluded(db, thread_id, turn_id, str(outcome.get("error") or ""))
        or _took_in_wake(db, turn_id)
        or _restart_resumes(db, thread_id, turn_id)
        or _person_input_queued(db, thread_id)
    ):
        return
    from events.delivery import enqueue_event_wake
    from infra.send_types import SendRejected

    try:
        enqueue_event_wake(
            thread_id, EVENT, SUMMARY, {"guidance": GUIDANCE},
            metadata={"kind": "unanswered_input", "failed_turn_id": turn_id},
            message_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"corsia-unanswered-input:{turn_id}")),
            db=db,
        )
    # A thread being deleted refuses it (SendRejected); the Turn-end record still commits.
    except (ValueError, SendRejected) as error:
        logger.warning("unanswered-input wake skipped for {}: {}", thread_id, error)
        return
    pending.drives.append(thread_id)
