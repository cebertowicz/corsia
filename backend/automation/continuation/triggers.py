"""Durable repository for bounded continuation trigger outcomes."""

from __future__ import annotations

import sqlite3
import uuid

from automation.continuation.models import (
    LIVE_STATES,
    REASONS,
    TERMINAL_STATES,
    Trigger,
    TriggerRequest,
)


def _row(row) -> Trigger:
    return Trigger(**{field: row[field] for field in Trigger.__dataclass_fields__})


def get(trigger_id: str, *, db) -> Trigger:
    """Return one durable trigger or raise for an unknown identity."""

    row = db.execute(
        "SELECT * FROM continuation_triggers WHERE id=?", (trigger_id,)
    ).fetchone()
    if row is None:
        raise KeyError(trigger_id)
    return _row(row)


def _dedupe_winner(request: TriggerRequest, db):
    """Return the live trigger this request would duplicate, if any."""
    return db.execute(
        """SELECT * FROM continuation_triggers
           WHERE dedupe_key=?
             AND state IN ('pending','dispatched','deferred')
           ORDER BY created_at DESC LIMIT 1""",
        (request.dedupe_key,),
    ).fetchone()


def enqueue(
    request: TriggerRequest,
    *,
    db,
    now: str,
) -> tuple[Trigger, bool]:
    """Insert one trigger or return its kind-specific durable dedupe winner."""

    existing = _dedupe_winner(request, db)
    if existing is not None:
        return _row(existing), False
    trigger_id = uuid.uuid4().hex
    try:
        db.execute(
            """INSERT INTO continuation_triggers
               (id,kind,thread_id,scope_id,subject_id,dedupe_key,state,
                cost_estimate_usd,next_attempt_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,'pending',?,?,?,?)""",
            (
                trigger_id,
                request.kind,
                request.thread_id,
                request.scope_id,
                request.subject_id,
                request.dedupe_key,
                request.cost_estimate_usd,
                request.not_before,
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError:
        existing = _dedupe_winner(request, db)
        if existing is None:
            raise
        return _row(existing), False
    return get(trigger_id, db=db), True


def claim(
    trigger_id: str,
    *,
    idempotency_key: str,
    db,
    now: str,
) -> bool:
    """Claim one due live trigger for dispatch exactly once."""

    cursor = db.execute(
        """UPDATE continuation_triggers
           SET state='dispatched', idempotency_key=?, attempt=attempt+1,
               dispatched_at=?, updated_at=?
           WHERE id=? AND state IN ('pending','deferred')
             AND (next_attempt_at IS NULL OR next_attempt_at<=?)""",
        (idempotency_key, now, now, trigger_id, now),
    )
    return cursor.rowcount == 1


def settle(
    trigger_id: str,
    *,
    outcome: str,
    reason: str,
    db,
    now: str,
) -> bool:
    """Settle one claimed trigger with one closed-vocabulary reason."""

    if outcome not in TERMINAL_STATES:
        raise ValueError(f"invalid terminal trigger state: {outcome}")
    if reason not in REASONS:
        raise ValueError(f"invalid continuation reason: {reason}")
    cursor = db.execute(
        """UPDATE continuation_triggers
           SET state=?, reason=?, settled_at=?, updated_at=?
           WHERE id=? AND state='dispatched' AND settled_at IS NULL""",
        (outcome, reason, now, now, trigger_id),
    )
    return cursor.rowcount == 1


def record_failed_accepted_delivery(
    idempotency_key: str,
    *,
    db,
    now: str,
) -> list[Trigger]:
    """Correct a claimed wake when its accepted input later fails admission.

    ``execution.deliver_one`` stores the exact event idempotency key on both the
    continuation trigger and the FIFO message. The message's settled
    ``delivery_state`` is canonical, so a later failed admission corrects the
    producer record instead of leaving it marked delivered. Returns each
    corrected trigger as it stood before the correction.
    """
    corrected = []
    for row in db.execute(
        """SELECT * FROM continuation_triggers
           WHERE idempotency_key=? AND state IN ('dispatched','delivered')""",
        (idempotency_key,),
    ).fetchall():
        cursor = db.execute(
            """UPDATE continuation_triggers
               SET state='failed', reason='delivery_failed', settled_at=?, updated_at=?
               WHERE id=? AND state=?""",
            (now, now, row["id"], row["state"]),
        )
        if cursor.rowcount == 1:
            corrected.append(_row(row))
    return corrected


def cancel_for_plan(
    plan_id: str,
    owner_thread_id: str,
    *,
    db,
    now: str,
) -> int:
    """Cancel unclaimed T0/T1/T2 continuation owned by one paused plan."""

    cursor = db.execute(
        """UPDATE continuation_triggers
           SET state='cancelled', reason='plan_paused', settled_at=?, updated_at=?
           WHERE state IN ('pending','deferred')
             AND (
               (kind='t0_liveness' AND thread_id=?)
               OR (
                 kind='t1_watchdog' AND subject_id IN (
                   SELECT id FROM plan_nodes WHERE plan_id=?
                 )
               )
               OR (
                 kind='t2_watch_expiry' AND subject_id IN (
                   SELECT id FROM expected_event_watches WHERE plan_id=?
                 )
               )
             )""",
        (now, now, owner_thread_id, plan_id, plan_id),
    )
    return cursor.rowcount


def cancel_for_watch(watch_id: str, *, db, now: str, reason: str) -> int:
    """Cancel the unclaimed T2 continuation of a watch that has left `armed`.

    Called by the write that resolves or expires the watch, so a settled wait
    leaves no deferred expiry behind for a later scan to find.
    """

    cursor = db.execute(
        """UPDATE continuation_triggers
           SET state='cancelled', reason=?, settled_at=?, updated_at=?
           WHERE state IN ('pending','deferred')
             AND kind='t2_watch_expiry' AND subject_id=?""",
        (reason, now, now, watch_id),
    )
    return cursor.rowcount


def cancel_retired_kinds(live_kinds: set[str], *, db, now: str) -> int:
    """Cancel live rows whose kind no longer has a source to settle them."""

    if not live_kinds:
        return 0
    marks = ",".join("?" for _ in live_kinds)
    cursor = db.execute(
        f"""UPDATE continuation_triggers
           SET state='cancelled', reason='source_retired', settled_at=?, updated_at=?
           WHERE state IN ('pending','dispatched','deferred')
             AND kind NOT IN ({marks})""",
        (now, now, *sorted(live_kinds)),
    )
    return cursor.rowcount


def recover_dispatch(
    trigger_id: str,
    *,
    delivered: bool,
    db,
    now: str,
    reason: str = "progress_unchanged",
) -> bool:
    """Settle or retry a crash-interrupted dispatched trigger."""

    if delivered:
        return settle(
            trigger_id,
            outcome="delivered",
            reason=reason,
            db=db,
            now=now,
        )
    cursor = db.execute(
        """UPDATE continuation_triggers
           SET state='pending', reason=NULL, next_attempt_at=NULL, updated_at=?
           WHERE id=? AND state='dispatched'""",
        (now, trigger_id),
    )
    return cursor.rowcount == 1
