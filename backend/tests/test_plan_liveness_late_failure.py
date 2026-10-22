"""A liveness wake whose accepted input later fails admission wakes again.

Delivering a T0 wake settles its source at dispatch: the row re-arms at its
next cycle. If the FIFO input it appended later fails admission (a dead model
binding, a lock), ``record_failed_accepted_delivery`` corrects the trigger to
``failed``. The trigger is the one record of what happened to that delivery;
the liveness row is only the schedule, and it must still wake the thread at
its next cycle, bounded by the cycle cap, rather than go silent.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from automation import plan_liveness
from automation.continuation import execution, settlement, triggers
from automation.continuation.sources.liveness import LivenessSource
from infra.schema import SCHEMA_SQL
from plans import admission, service

KEY = "wake-idempotency-key"


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('owner','user','test/model','t','t')"
    )
    db.commit()
    return db


def _actionable(db) -> None:
    plan = service.ensure_plan("thread", "owner", db=db)
    task = service.add_node(
        plan.id, kind="task", title="Draft the brief",
        acceptance_criteria="The brief is written", db=db,
    )
    service.transition_node(task.id, "active", db=db)
    admission.select(task.id, actor_id="owner", provenance="test", db=db, touch=False)
    db.commit()


def _later(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _deliver(db, now: str) -> str:
    """Claim and settle the due wake as delivered, as dispatch does."""
    source = LivenessSource()
    request = source.due(now, db=db)[0]
    trigger, created = triggers.enqueue(request, db=db, now=now)
    assert created
    assert triggers.claim(trigger.id, idempotency_key=KEY, db=db, now=now)
    db.commit()
    settled = settlement.settle_claimed(
        trigger.id, "delivered", request.reason, source, db=db, now=now,
    )
    assert settled is not None
    return trigger.id


def _wake(db) -> sqlite3.Row:
    return db.execute(
        "SELECT status,cycle FROM plan_liveness_wakes WHERE thread_id='owner'"
    ).fetchone()


def test_late_admission_failure_leaves_the_next_cycle_armed():
    db = _db()
    try:
        _actionable(db)
        plan_liveness.on_turn_complete("owner", db=db)
        db.commit()
        trigger_id = _deliver(db, _later(1))
        assert (_wake(db)["status"], _wake(db)["cycle"]) == ("armed", 1)

        execution.record_failed_accepted_delivery(KEY, db=db, now=_later(1))
        db.commit()

        trigger = triggers.get(trigger_id, db=db)
        assert (trigger.state, trigger.reason) == ("failed", "delivery_failed")
        assert (_wake(db)["status"], _wake(db)["cycle"]) == ("armed", 1)
        again = LivenessSource().due(_later(2), db=db)
        assert [request.thread_id for request in again] == ["owner"]
        assert again[0].outcome is None
    finally:
        db.close()


def test_repeated_late_failures_never_stop_the_wake():
    db = _db()
    try:
        _actionable(db)
        plan_liveness.on_turn_complete("owner", db=db)
        db.commit()
        for hours in range(1, 7):
            _deliver(db, _later(hours))
            execution.record_failed_accepted_delivery(KEY, db=db, now=_later(hours))
            db.execute(
                "UPDATE continuation_triggers SET idempotency_key=NULL "
                "WHERE idempotency_key=?", (KEY,),
            )
            db.commit()

        last = LivenessSource().due(_later(24), db=db)

        assert [(r.outcome, r.reason) for r in last] == [(None, "progress_unchanged")]
    finally:
        db.close()
