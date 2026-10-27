"""The one write that gives a settled input batch its durable order and state."""

from __future__ import annotations

import sqlite3


async def settle_load_failure(batch, error, *, attempt_id: str | None = None) -> bool:
    """Settle a person's failed load and publish it; False for any other error."""
    from infra.db_async import run_in_db
    from infra.failed_wake_surface import drive_failed_wake_owners
    from infra.send_broadcast import broadcast_message_rows
    from llm.local_model_load import ModelLoadFailed

    if not isinstance(error, ModelLoadFailed):
        return False
    failed = await run_in_db(_settle_load_failure, batch, error, attempt_id)
    await broadcast_message_rows(batch.thread_id, tuple(dict.fromkeys((*(i.message_id for i in batch.items), *failed))))
    await drive_failed_wake_owners(failed)
    return True


def _settle_load_failure(batch, error, attempt_id: str | None, db=None) -> tuple[str, ...]:
    """In one write, fail the load's people (fitting-released ones too) and requeue the rest."""
    from dataclasses import replace
    from infra.database import write_transaction
    from infra.input_writes import release_batch_after_proved_no_write, release_batch_before_write
    from infra.send_target import model_load_error

    authors = batch.load_for
    rest = replace(batch, items=tuple(item for item in batch.items if item.message_id not in authors))
    with write_transaction(db, scope="settle_load_failure") as connection:
        if rest.items and attempt_id is None:
            release_batch_before_write(rest, db=connection)
        elif rest.items:
            release_batch_after_proved_no_write(rest, attempt_id=attempt_id, error=None, db=connection)
        # Rows stopped, deleted or claimed by another Turn meanwhile are left alone.
        rows = sorted(connection.execute(
            "UPDATE messages SET metadata=json_set(COALESCE(metadata, '{}'), '$.model_load_cause', ?) "
            f"WHERE id IN ({','.join('?' * len(authors))}) AND delivery_state='queued' "
            "AND (active_turn_id IS NULL OR active_turn_id=?) RETURNING id,thread_id,input_order",
            (error.cause, *authors, batch.turn_id),
        ).fetchall(), key=lambda row: row['input_order'])
        if not rows:
            return ()
        return sequence_attempt(
            connection, rows, state="failed", outcome="proved_no_write", error=model_load_error(error.cause),
        )


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
