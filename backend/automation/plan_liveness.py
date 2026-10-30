"""Exactly-once liveness wakes backed only by structured plan state."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from infra.database import get_db, write_transaction
from automation.continuation.progress import (
    ADVANCED,
    UNCHANGED,
    ProgressSnapshot,
    classify_progress,
)

if TYPE_CHECKING:
    # plans.lifecycle imports this module, so plans stays out of import time.
    from plans.public import PlanLivenessSignal

PLAN_WAKE_IDLE_SECONDS = 180
PLAN_WAKE_BACKOFF = 2.0
#: Production (2026-10-08..22) long Turns: p50 464s, p75 902s, p90 1,690s;
#: 600s gives a median long Turn about one reminder and a p90 about three.
PLAN_WAKE_IN_TURN_SECONDS = 600

ARMED = "armed"
CANCELLED = "cancelled"
STALLED = "stalled"


@dataclass(frozen=True)
class PlanWake:
    """One durable liveness wake projection."""

    id: str
    thread_id: str
    status: str
    deadline: str
    cycle: int
    last_open_count: int
    last_signal_hash: str
    last_settled_events: int
    provider_reset_at: str | None
    provider_reset_turn_id: str | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def row_to_wake(row: dict) -> PlanWake:
    """Project one stored liveness row."""
    return PlanWake(
        id=row["id"],
        thread_id=row["thread_id"],
        status=row["status"],
        deadline=row["deadline"],
        cycle=row["cycle"],
        last_open_count=row["last_open_count"],
        last_signal_hash=row["last_signal_hash"],
        last_settled_events=row["last_settled_events"],
        provider_reset_at=row["provider_reset_at"],
        provider_reset_turn_id=row["provider_reset_turn_id"],
    )


def wake_for(thread_id: str, db) -> dict | None:
    """Return the thread's one liveness row, if any."""
    return db.execute(
        "SELECT * FROM plan_liveness_wakes WHERE thread_id=?", (thread_id,),
    ).fetchone()


def _progress(signal: PlanLivenessSignal, wake) -> str:
    """Classify one signal against the stored wake snapshot."""

    snapshot = ProgressSnapshot(
        wake["last_open_count"],
        wake["last_signal_hash"],
        wake["last_settled_events"],
    )
    return classify_progress(signal, snapshot)


def arm_plan_wake(
    thread_id: str,
    signal: PlanLivenessSignal,
    *,
    cycle: int | None = None,
    db=None,
) -> PlanWake | None:
    """Arm one wake, refreshing its deadline only on structured progress.

    One episode per actionable state: a stalled wake stays stalled while the
    state is unchanged, so an unrelated Turn never re-arms the same stall.
    """

    if signal.count <= 0:
        return None
    with write_transaction(db, scope="arm_plan_wake") as connection:
        current = wake_for(thread_id, connection)
        if (
            current is not None
            and current["status"] == ARMED
            and current["provider_reset_at"] is not None
        ):
            return row_to_wake(dict(current))
        if cycle is not None:
            effective_cycle = cycle
        elif current is None or current["status"] not in (ARMED, STALLED):
            effective_cycle = 0
        else:
            progress = _progress(signal, current)
            if current["status"] == STALLED and progress == UNCHANGED:
                return None
            if current["status"] == ARMED and progress != ADVANCED:
                return row_to_wake(dict(current))
            effective_cycle = 0
        deadline = _now() + timedelta(
            seconds=PLAN_WAKE_IDLE_SECONDS * (PLAN_WAKE_BACKOFF**effective_cycle)
        )
        return write_row(
            connection, thread_id, signal, deadline=deadline, cycle=effective_cycle,
        )


def write_row(
    connection,
    thread_id: str,
    signal: PlanLivenessSignal,
    *,
    deadline: datetime,
    cycle: int,
    reset_turn_id: str | None = None,
) -> PlanWake:
    """Write the thread's one liveness row; the only insert or upsert of it.

    With ``reset_turn_id`` the deadline is a provider reset refused by that
    Turn. Without it the row is generic backoff: any reset time is cleared,
    and the last refused Turn stays as the replay guard.
    """
    deadline_iso = _iso(deadline.astimezone(timezone.utc))
    now = _iso(_now())
    connection.execute(
        """INSERT INTO plan_liveness_wakes
           (id,thread_id,status,deadline,cycle,last_open_count,
            last_signal_hash,last_settled_events,armed_at,updated_at,
            provider_reset_at,provider_reset_turn_id)
           VALUES (?,?,'armed',?,?,?,?,?,?,?,?,?)
           ON CONFLICT(thread_id) DO UPDATE SET
             status='armed',deadline=excluded.deadline,
             cycle=excluded.cycle,last_open_count=excluded.last_open_count,
             last_signal_hash=excluded.last_signal_hash,
             last_settled_events=excluded.last_settled_events,
             fired_at=NULL,updated_at=excluded.updated_at,
             provider_reset_at=excluded.provider_reset_at,
             provider_reset_turn_id=COALESCE(
               excluded.provider_reset_turn_id,
               plan_liveness_wakes.provider_reset_turn_id)""",
        (
            str(uuid.uuid4())[:12], thread_id, deadline_iso, cycle,
            signal.count, signal.signal_hash, signal.settled_events, now, now,
            deadline_iso if reset_turn_id else None, reset_turn_id,
        ),
    )
    armed = wake_for(thread_id, connection)
    assert armed is not None, "the upsert above always leaves one row"
    return row_to_wake(dict(armed))


def clear_provider_reset(thread_id: str, *, db) -> bool:
    """End an armed reset wait, keeping its Turn as the replay guard."""
    cursor = db.execute(
        """UPDATE plan_liveness_wakes
           SET provider_reset_at=NULL,updated_at=?
           WHERE thread_id=? AND status='armed' AND provider_reset_at IS NOT NULL""",
        (_iso(_now()), thread_id),
    )
    return cursor.rowcount == 1


def cancel_plan_wake(thread_id: str, *, db=None) -> bool:
    """Cancel an armed or stalled wake, ending its episode; may join a caller transaction."""

    with write_transaction(db, scope="cancel_plan_wake") as connection:
        cursor = connection.execute(
            "UPDATE plan_liveness_wakes "
            "SET status='cancelled',provider_reset_at=NULL,updated_at=? "
            "WHERE thread_id=? AND status IN ('armed','stalled')",
            (_iso(_now()), thread_id),
        )
        return cursor.rowcount >= 1


def on_turn_complete(
    thread_id: str, *, cycle: int | None = None, db=None,
) -> PlanWake | None:
    """Read structured state and update its wake in the same caller-owned write.

    Called wherever plan state changed enough to re-arm liveness, which is not
    only at a Turn boundary. Use
    ``plan_liveness_terminal.on_thread_turn_complete`` for the real
    end of a thread's Turn: what is true only there belongs behind that seam,
    not here where a mid-Turn watch or archive write would also trip it.
    """

    from plans.public import liveness_signal

    with write_transaction(db, scope="plan_turn_complete") as connection:
        signal = liveness_signal(thread_id, db=connection)
        if signal.count <= 0:
            cancel_plan_wake(thread_id, db=connection)
            return None
        return arm_plan_wake(thread_id, signal, cycle=cycle, db=connection)


def due_plan_wakes(now_iso: str, *, db=None) -> list[PlanWake]:
    """Return armed wakes with a valid deadline at or before the supplied time."""

    connection = db or get_db()
    try:
        rows = connection.execute(
            "SELECT * FROM plan_liveness_wakes "
            "WHERE status='armed' AND deadline<=?",
            (now_iso,),
        ).fetchall()
        due: list[PlanWake] = []
        for row in rows:
            try:
                datetime.fromisoformat(row["deadline"])
            except (ValueError, TypeError):
                continue
            due.append(row_to_wake(dict(row)))
        return due
    finally:
        if not db:
            connection.close()


def _claim_fire(wake_id: str, new_status: str, db) -> bool:
    """Atomically claim one armed wake into a terminal state."""

    cursor = db.execute(
        "UPDATE plan_liveness_wakes "
        "SET status=?,provider_reset_at=NULL,fired_at=?,updated_at=? "
        "WHERE id=? AND status='armed'",
        (new_status, _iso(_now()), _iso(_now()), wake_id),
    )
    return cursor.rowcount == 1


def _rearm_due_wake(
    wake: PlanWake,
    signal: PlanLivenessSignal,
    cycle: int,
    db,
    *,
    seconds: float | None = None,
) -> bool:
    """Move the exact due wake to its next deadline without a claim gap.

    ``seconds`` overrides the idle backoff spacing (the flat in-Turn interval).
    """

    if signal.count <= 0:
        return False
    now = _now()
    if seconds is None:
        seconds = PLAN_WAKE_IDLE_SECONDS * (PLAN_WAKE_BACKOFF**cycle)
    deadline = _iso(now + timedelta(seconds=seconds))
    cursor = db.execute(
        """UPDATE plan_liveness_wakes
           SET status='armed',deadline=?,cycle=?,last_open_count=?,
               last_signal_hash=?,last_settled_events=?,fired_at=NULL,updated_at=?,
               provider_reset_at=NULL
           WHERE id=? AND thread_id=? AND status='armed' AND deadline=?
             AND cycle=? AND last_open_count=? AND last_signal_hash=?
             AND last_settled_events=?""",
        (
            deadline,
            cycle,
            signal.count,
            signal.signal_hash,
            signal.settled_events,
            _iso(now),
            wake.id,
            wake.thread_id,
            wake.deadline,
            wake.cycle,
            wake.last_open_count,
            wake.last_signal_hash,
            wake.last_settled_events,
        ),
    )
    return cursor.rowcount == 1


async def sweep_plan_wakes(
    now_iso: str,
    *,
    db=None,
    signal_resolver=None,
    deliver=None,
) -> dict:
    """Compatibility adapter onto the single continuation runner."""

    from automation.continuation.runner import sweep
    from automation.continuation.sources.liveness import LivenessSource

    return await sweep(
        now_iso,
        db=db,
        sources=[LivenessSource(signal_resolver)],
        deliver=deliver,
    )


async def plan_liveness_sweep_loop() -> None:
    """Compatibility adapter onto the single continuation loop."""

    from automation.continuation.loop import continuation_sweep_loop

    await continuation_sweep_loop()
