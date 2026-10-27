"""The one write that gives a settled input batch its durable order and state."""

from __future__ import annotations

import sqlite3


def delivery_error(error) -> str | None:
    """Map a failure to its durable delivery code."""
    from infra.send_target import model_load_error
    from llm.local_model_load import ModelLoadFailed

    if isinstance(error, ModelLoadFailed):
        return model_load_error(error.cause)
    return str(error) if error is not None else None


def failed_delivery_error(db, rows, error) -> str | None:
    """Stamp a model-load cause at failure and return its delivery code."""
    from llm.local_model_load import ModelLoadFailed

    if isinstance(error, ModelLoadFailed):
        db.executemany(
            "UPDATE messages SET metadata=json_set(COALESCE(metadata, '{}'), '$.model_load_cause', ?) WHERE id=?",
            [(error.cause, row['id']) for row in rows],
        )
    return delivery_error(error)


def release_background_on_load_failure(batch, error, *, attempt_id=None, db=None) -> None:
    """Release non-person rows when a person's model load fails before writing."""
    from dataclasses import replace
    from core.message_history import is_human_input
    from infra.input_writes import release_batch_after_proved_no_write, release_batch_before_write
    from llm.local_model_load import ModelLoadFailed

    if not isinstance(error, ModelLoadFailed) or not batch.may_load:
        return
    background = tuple(item for item in batch.items if not is_human_input(item.role, item.metadata))
    if not background:
        return
    subset = replace(batch, items=background)
    if attempt_id is None:
        release_batch_before_write(subset, db=db)
    else:
        release_batch_after_proved_no_write(subset, attempt_id=attempt_id, error=None, db=db)


def sequence_attempt(
    db: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    state: str,
    outcome: str,
    error: str | None,
) -> tuple[str, ...]:
    """Give one settled batch its durable order and delivery state.

    Reports each wake it ended ``failed`` in this transaction and returns their
    ids so the owning frame can drive the report owner after commit. A batch
    proved unwritten offers its marked passive notes again.
    """
    thread_id = str(rows[0]["thread_id"])
    next_seq = int(db.execute(
        "SELECT next_message_seq FROM threads WHERE id=?", (thread_id,),
    ).fetchone()[0])
    for offset, row in enumerate(rows):
        db.execute(
            "UPDATE messages SET delivery_state=?,write_outcome=?,"
            "delivery_error=?,next_attempt_at=NULL,seq=?,"
            "delivery_revision=delivery_revision+1 WHERE id=?",
            (state, outcome, error, next_seq + offset, row["id"]),
        )
    db.execute(
        "UPDATE threads SET next_message_seq=? WHERE id=?",
        (next_seq + len(rows), thread_id),
    )
    if outcome == "proved_no_write":
        from infra.passive_changes import reoffer_unwritten_notes

        reoffer_unwritten_notes(db, tuple(str(row["id"]) for row in rows))
    if state != "failed":
        return ()
    from infra.failed_wake_surface import record_failed_wakes

    failed = tuple(str(row["id"]) for row in rows)
    record_failed_wakes(db, failed)
    return failed
