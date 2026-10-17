"""Ending a queued input, and judging whether a plan wake still has work.

Two writes that belong together: the one that ends an unclaimed queued input in
place, and the judgement that decides a plan wake has nothing left to wake for.
A user's cancellation and a wake whose work moved on leave the same durable,
inspectable row, each named by its own ``delivery_error``.

``infra.thread_inputs`` owns FIFO selection; this module owns each exact
queued-row ending.
"""

from __future__ import annotations

import json
import sqlite3

from infra.database import write_transaction


def refresh_account_input_holds(db, thread_id):
    """Mark eligible unwritten inputs; only a canonical claim clears provenance."""
    from connections.account_holds import ACCOUNT_HOLD_REASONS, model_account_hold

    if not db.in_transaction:
        raise RuntimeError("account input holds require the owner's writer transaction")
    # Read the prior causes/outcomes in FIFO order before marking, including NULLs.
    pending = db.execute("SELECT id,target_model,delivery_error,write_outcome FROM messages WHERE thread_id=? "
                         "AND input_order IS NOT NULL AND delivery_state='queued' AND active_turn_id IS NULL AND write_started_at IS NULL "
                         "AND delivery_attempt_id IS NULL AND (write_outcome IS NULL OR write_outcome='proved_no_write') ORDER BY input_order",
                         (thread_id,)).fetchall()
    for item in pending:
        if item["delivery_error"] is not None and item["delivery_error"] not in ACCOUNT_HOLD_REASONS:
            continue
        reason = model_account_hold(thread_id, str(item["target_model"] or ""), db=db)
        if reason and (item['delivery_error'] != reason or item['write_outcome'] != 'proved_no_write'):
            db.execute("UPDATE messages SET write_outcome='proved_no_write',delivery_error=? WHERE id=?",
                       (reason, item["id"]))
    offer_account_head_notice(db, thread_id)


def offer_account_head_notice(db, thread_id):
    """Offer the eligible head to the existing episode/dedup owner, never a readiness wake."""
    from connections import read_account_readiness
    from plans.public import notify_turn_error, stalled_why

    if not db.in_transaction:
        raise RuntimeError('account head notice requires the owning writer transaction')
    if stalled_why(db, thread_id) != 'account':
        return
    readiness = read_account_readiness([thread_id], db=db).get(thread_id)
    if readiness is not None and readiness['ready']:
        return
    head = db.execute("SELECT delivery_error FROM messages WHERE thread_id=? AND input_order IS NOT NULL "
                      "AND delivery_state='queued' AND active_turn_id IS NULL ORDER BY input_order LIMIT 1",
                      (thread_id,)).fetchone()
    if head is not None:
        notify_turn_error(db, thread_id, turn_id=None, error=head['delivery_error'])


def _settle_queued(
    connection: sqlite3.Connection,
    thread_id: str,
    rows: list[tuple[str, int]],
    *,
    state: str,
    error: str,
) -> tuple[str, ...]:
    """End exact unclaimed rows in FIFO order through the existing delivery states."""
    if not rows:
        return ()
    next_seq = int(connection.execute(
        "SELECT next_message_seq FROM threads WHERE id=?", (thread_id,),
    ).fetchone()[0])
    for offset, (message_id, revision) in enumerate(rows):
        updated = connection.execute(
            "UPDATE messages SET delivery_state=?,delivery_error=?,"
            "write_outcome='proved_no_write',next_attempt_at=NULL,"
            "delivery_revision=delivery_revision+1,seq=? "
            "WHERE id=? AND thread_id=? AND delivery_revision=? "
            "AND delivery_state='queued' AND active_turn_id IS NULL",
            (state, error, next_seq + offset, message_id, thread_id, revision),
        )
        if updated.rowcount != 1:
            raise RuntimeError(f"queued input changed before settlement: {message_id}")
    connection.execute(
        "UPDATE threads SET next_message_seq=? WHERE id=?",
        (next_seq + len(rows), thread_id),
    )
    ids = tuple(message_id for message_id, _revision in rows)
    from infra.passive_changes import reoffer_unwritten_notes

    reoffer_unwritten_notes(connection, ids)
    if state == "failed":
        from infra.failed_wake_surface import record_failed_wakes

        record_failed_wakes(connection, ids)
    offer_account_head_notice(connection, thread_id)
    return ids


def cancel_queued(
    connection: sqlite3.Connection,
    thread_id: str,
    message_id: str,
    revision: int,
    *,
    error: str,
) -> None:
    """Cancel one unclaimed queued input in place, keeping its visible row."""
    _settle_queued(
        connection,
        thread_id,
        [(message_id, revision)],
        state="cancelled",
        error=error,
    )


def fail_queued(
    connection: sqlite3.Connection,
    thread_id: str,
    rows: list[sqlite3.Row],
    *,
    error: str,
) -> tuple[str, ...]:
    """Fail one exact unclaimed FIFO prefix, keeping every row visible and retryable."""
    return _settle_queued(
        connection,
        thread_id,
        [(str(row["id"]), int(row["delivery_revision"])) for row in rows],
        state="failed",
        error=error,
    )



def still_claimable(
    connection: sqlite3.Connection, thread_id: str, rows: list[sqlite3.Row],
) -> list[sqlite3.Row]:
    """Drop the queued plan wakes whose work has moved on, cancelling each.

    A wake is appended while its source still describes work that can move. By
    the time a Turn would claim it, that work can have been closed, released or
    made to wait, and a Turn started for it would have nothing to do. Judging
    the condition again inside the claim's own write is what keeps that Turn
    from starting; the wake stays visible as a cancelled row.
    """
    from automation.continuation.validity import is_claimable

    current: list[sqlite3.Row] = []
    for row in rows:
        condition = row["continuation_condition"]
        if condition and not is_claimable(
            thread_id, json.loads(condition), db=connection,
        ):
            cancel_queued(
                connection, thread_id, str(row["id"]),
                int(row["delivery_revision"]), error="stale_plan_wake",
            )
            continue
        current.append(row)
    return current


def cancel_queued_input(
    thread_id: str,
    message_id: str,
    *,
    revision: int,
    db: sqlite3.Connection | None = None,
) -> sqlite3.Row:
    """Cancel one exact unclaimed FIFO input without deleting its visible row."""
    with write_transaction(db, scope="cancel_queued_input") as connection:
        row = connection.execute(
            "SELECT * FROM messages WHERE id=? AND thread_id=?",
            (message_id, thread_id),
        ).fetchone()
        if row is None:
            raise ValueError("input does not exist")
        if (
            row["delivery_state"] != "queued"
            or row["active_turn_id"] is not None
            or row["write_started_at"] is not None
            or int(row["delivery_revision"]) != revision
        ):
            raise RuntimeError("input is no longer an unclaimed queued message")
        cancel_queued(
            connection, thread_id, message_id, revision, error="cancelled_by_user",
        )
        return connection.execute(
            "SELECT * FROM messages WHERE id=?", (message_id,),
        ).fetchone()
