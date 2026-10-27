"""References and next-step projections for turn-ending errors.

The reference comes from `infra.failure_reference.new_error_reference`, the one
generator, and readers accept it through that module's `REFERENCE`. `next_step` is never stored:

* `recovered`: `error_recovery` stamped the row when the agent moved past it.
* `held`: the row ended the thread's latest Turn, that Turn failed, no Turn has
  started since, and an input is queued. Retry would duplicate that input; why it
  waits (a model, account or locality hold, or nothing) is the hold's own surface.
* `failed`: anything else, including an error the person already answered with a
  new message (a later Turn exists, so the error is no longer the latest outcome).
"""

from __future__ import annotations

from typing import Callable

from loguru import logger

from infra.failure_reference import new_error_reference
from infra.focus_rows import ERROR_SYSTEM_TYPES

# The thread's latest Turn: one index seek, so a thread whose latest Turn did
# not fail never reads its messages.
_LATEST_TURN_SQL = (
    "SELECT id, status, started_at FROM active_turns WHERE thread_id=? "
    "ORDER BY rowid DESC LIMIT 1"
)
# The newest error row, read only once that Turn failed, and only among rows
# written since the second it started: a failed Turn with no error row (a
# provider `result` failure) reads that Turn's rows, never the whole thread.
# Comparing to the second keeps naive and '+00:00' UTC stamps comparable.
_LAST_TURN_ERROR_SQL = (
    "SELECT id, json_extract(metadata, '$.turn_id') AS turn_id FROM messages "
    "WHERE thread_id=? AND created_at >= substr(?, 1, 19) AND role='system' "
    "AND seq IS NOT NULL AND json_valid(metadata) "
    "AND json_extract(metadata, '$.type') IN ({placeholders}) "
    "ORDER BY created_at DESC LIMIT 1"
)


def needs_reference(metadata: dict) -> bool:
    """Whether a row ends its Turn as a system failure a person may quote.

    The error kinds, and a provider `result` that failed for a reason other
    than the person's Stop.
    """
    kind = metadata.get("type")
    if kind in ERROR_SYSTEM_TYPES:
        return True
    return (
        kind == "result" and metadata.get("is_error") is True
        and metadata.get("stop_reason") != "interrupted"
    )


def insert_turn_error(
    insert_message_fn: Callable,
    thread_id: str,
    content: str,
    metadata: dict,
    *,
    turn_id: str | None,
    msg_id: str,
    db=None,
    created_at: str | None = None,
) -> str:
    """Persist one terminal error with its stable operator-facing reference."""
    metadata["turn_id"] = turn_id
    metadata["reference"] = new_error_reference()
    message_id = insert_message_fn(
        thread_id, "system", content, metadata, msg_id=msg_id, db=db,
        created_at=created_at,
    )
    logger.info(
        "turn_error reference={} turn_id={} thread={} kind={}",
        metadata["reference"], turn_id, thread_id, metadata.get("type"),
    )
    return message_id


def apply_error_next_steps(db, thread_id: str, messages: list[dict]) -> None:
    """Add transient retry guidance to projected terminal-error rows in place."""
    error_rows = [
        row for row in messages
        if isinstance(row.get("metadata"), dict)
        and row["metadata"].get("type") in ERROR_SYSTEM_TYPES
    ]
    if not error_rows:
        return

    held_id = last_turn_error_id(db, thread_id)
    queued = held_id is not None and db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND input_order IS NOT NULL "
        "AND delivery_state='queued' LIMIT 1",
        (thread_id,),
    ).fetchone() is not None

    for row in error_rows:
        metadata = row["metadata"]
        if metadata.get("recovered") is True:
            metadata["next_step"] = "recovered"
        elif str(row.get("id")) == held_id and queued:
            metadata["next_step"] = "held"
        else:
            metadata["next_step"] = "failed"


def last_turn_error_id(db, thread_id: str) -> str | None:
    """The error that ended the thread's latest, failed Turn; the only row 'held' can apply to."""
    turn = db.execute(_LATEST_TURN_SQL, (thread_id,)).fetchone()
    if turn is None or turn[1] != "error":
        return None
    placeholders = ",".join("?" for _ in ERROR_SYSTEM_TYPES)
    row = db.execute(
        _LAST_TURN_ERROR_SQL.format(placeholders=placeholders),
        (thread_id, turn[2], *ERROR_SYSTEM_TYPES),
    ).fetchone()
    return None if row is None or row[1] != turn[0] else str(row[0])
