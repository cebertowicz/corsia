"""Provider-reset deadlines stored in the existing per-thread liveness row."""

from __future__ import annotations

from datetime import datetime

from infra.database import write_transaction


def deadline(
    error: str,
    *,
    clears_at: str | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """Return the strict reset deadline shared by every terminal harness path."""
    from subagents.provider_usage_limits import provider_reset_deadline

    return provider_reset_deadline(error, reset_at=clears_at, now=now)


def nodes(thread_ids: set[str], *, db) -> dict[str, dict]:
    """Return reset waits and their terminal selection in bounded batches."""
    result: dict[str, dict] = {}
    ordered = sorted(thread_ids)
    for start in range(0, len(ordered), 900):
        batch = ordered[start:start + 900]
        marks = ",".join("?" for _ in batch)
        rows = db.execute(
            f"""SELECT w.thread_id,w.provider_reset_at,w.provider_reset_turn_id,
                         t.plan_id,t.plan_node_id,t.plan_attempt_id
                  FROM plan_liveness_wakes AS w
                  JOIN active_turns AS t ON t.id=w.provider_reset_turn_id
                 WHERE w.thread_id IN ({marks}) AND w.status='armed'
                   AND w.provider_reset_at IS NOT NULL AND t.status='error'
                   AND NOT EXISTS (SELECT 1 FROM active_turns AS later
                                    WHERE later.thread_id=t.thread_id
                                      AND later.turn_order>t.turn_order)""",
            batch,
        ).fetchall()
        result.update({
            str(row["thread_id"]): dict(row)
            for row in rows
            if row["plan_id"] is not None and row["plan_node_id"] is not None
        })
    return result


def latest_turn(thread_id: str, turn_id: str, outcome: str, db):
    """Return the Turn only while it is the thread's latest terminal Turn.

    A replayed callback for an older Turn therefore changes nothing.
    """
    return db.execute(
        """SELECT t.plan_id,t.plan_node_id,t.plan_attempt_id FROM active_turns AS t
           WHERE t.id=? AND t.thread_id=? AND t.status=?
             AND NOT EXISTS (SELECT 1 FROM active_turns AS later
                              WHERE later.thread_id=t.thread_id
                                AND later.turn_order>t.turn_order
                                AND later.status IN ('completed','error','cancelled'))""",
        (turn_id, thread_id, outcome),
    ).fetchone()


def selection(thread_id: str, turn_id: str, outcome: str, db):
    """Return the exact still-valid selection carried by one terminal Turn."""
    from plans import public

    turn = latest_turn(thread_id, turn_id, outcome, db)
    if turn is None or any(turn[key] is None for key in turn.keys()):
        return None
    attempt = db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?", (turn["plan_attempt_id"],),
    ).fetchone()
    if attempt is None or (
        str(attempt["plan_id"]) != str(turn["plan_id"])
        or str(attempt["node_id"]) != str(turn["plan_node_id"])
        or str(attempt["executor_thread_id"]) != thread_id
    ):
        return None
    return attempt if public.selection_valid(db, attempt) else None


def arm(thread_id: str, *, turn_id: str, reset_at: datetime, db=None):
    """Replace generic liveness with one validated provider-reset deadline."""
    from automation import plan_liveness as liveness
    from plans.public import liveness_signal

    with write_transaction(db, scope="arm_provider_reset_wake") as connection:
        if selection(thread_id, turn_id, "error", connection) is None:
            return None
        signal = liveness_signal(thread_id, db=connection)
        if signal.count <= 0:
            liveness.cancel_plan_wake(thread_id, db=connection)
            return None
        current = liveness.wake_for(thread_id, connection)
        if current is not None and current["provider_reset_turn_id"] == turn_id:
            return liveness.row_to_wake(dict(current))
        return liveness.write_row(
            connection, thread_id, signal, deadline=reset_at, cycle=0,
            reset_turn_id=turn_id,
        )
