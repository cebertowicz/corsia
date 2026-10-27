"""The one write that gives a settled input batch its durable order and state."""

from __future__ import annotations

import sqlite3


def delivery_error(db, rows, error) -> str | None:
    """Keep a local load cause beside its durable delivery code."""
    from infra.send_target import model_load_error
    from llm.local_model_load import ModelLoadFailed

    if isinstance(error, ModelLoadFailed):
        db.executemany("UPDATE messages SET metadata=json_set(COALESCE(metadata, '{}'), '$.model_load_cause', ?) WHERE id=?",
                       [(error.cause, row['id']) for row in rows])
        return model_load_error(error.cause)
    return str(error) if error is not None else None


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
