"""Bounded clock/cadence schedules on the one continuation runner."""

from __future__ import annotations

import sqlite3

import pytest

from automation import schedules
from automation.continuation import runner, triggers
from automation.continuation.sources.schedules import ScheduleSource
from infra.database import write_transaction
from infra.schema import SCHEMA_SQL


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        """INSERT INTO threads
           (id,user_id,created_at,updated_at)
           VALUES ('thread','user','t','t')"""
    )
    db.commit()
    return db


async def _delivered(*_args) -> bool:
    return True


@pytest.mark.asyncio
async def test_cadence_enters_shared_runner_and_exhausts_at_max_runs():
    db = _db()
    schedule = schedules.create(
        "thread",
        "__workspace__",
        "thread",
        kind="cadence",
        spec="60",
        max_runs=1,
        now="2026-01-01T00:00:00+00:00",
        db=db,
    )

    result = await runner.sweep(
        "2026-01-01T00:01:00+00:00",
        db=db,
        deliver=_delivered,
    )

    assert schedule.id
    assert result["woken"] == ["thread"]
    row = db.execute(
        "SELECT state,runs FROM automation_schedules WHERE id=?",
        (schedule.id,),
    ).fetchone()
    assert dict(row) == {"state": "exhausted", "runs": 1}
    trigger = db.execute(
        """SELECT kind,state,reason FROM continuation_triggers
           WHERE subject_id=?""",
        (schedule.id,),
    ).fetchone()
    assert dict(trigger) == {
        "kind": "automation_clock",
        "state": "delivered",
        "reason": "schedule_due",
    }


def test_paused_schedule_produces_nothing_and_actor_owns_cancel_list():
    db = _db()
    schedule = schedules.create(
        "thread",
        "__workspace__",
        "thread",
        kind="clock",
        spec="2026-01-01T00:01:00+00:00",
        max_runs=1,
        now="2026-01-01T00:00:00+00:00",
        db=db,
    )
    db.execute(
        "UPDATE automation_schedules SET state='paused' WHERE id=?",
        (schedule.id,),
    )
    db.commit()

    assert ScheduleSource().due(
        "2026-01-01T00:02:00+00:00",
        db=db,
    ) == []
    assert schedules.cancel(
        schedule.id,
        thread_id="thread",
        actor_id="other",
        db=db,
    ) is False
    assert schedules.list_for_actor(
        thread_id="thread",
        actor_id="other",
        db=db,
    ) == []
    assert schedules.cancel(
        schedule.id,
        thread_id="thread",
        actor_id="thread",
        db=db,
    ) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "spec", "max_runs", "expected"),
    [
        (
            "clock",
            "2026-01-01T00:01:00+00:00",
            1,
            {
                "state": "exhausted",
                "runs": 1,
                "next_run_at": "2026-01-01T00:01:00+00:00",
            },
        ),
        (
            "cadence",
            "60",
            2,
            {
                "state": "active",
                "runs": 1,
                "next_run_at": "2026-01-01T00:02:00+00:00",
            },
        ),
    ],
)
async def test_failed_schedule_delivery_advances_occurrence_once(
    kind, spec, max_runs, expected,
):
    db = _db()
    schedule = schedules.create(
        "thread", "__workspace__", "thread", kind=kind, spec=spec,
        max_runs=max_runs, now="2026-01-01T00:00:00+00:00", db=db,
    )

    result = await runner.sweep(
        "2026-01-01T00:01:00+00:00",
        db=db, deliver=lambda *_args: False,
    )

    row = db.execute(
        "SELECT state,runs,next_run_at FROM automation_schedules WHERE id=?",
        (schedule.id,),
    ).fetchone()
    assert result["errored"] == ["thread"]
    assert dict(row) == expected

    repeated = await runner.sweep(
        "2026-01-01T00:01:00+00:00",
        db=db, deliver=lambda *_args: False,
    )
    assert repeated["errored"] == []
    assert db.execute(
        "SELECT COUNT(*) FROM continuation_triggers WHERE subject_id=?",
        (schedule.id,),
    ).fetchone()[0] == 1


@pytest.mark.parametrize("outcome", ["stalled", "cancelled", "suppressed"])
def test_unrun_cadence_occurrence_advances_to_the_next(outcome):
    db = _db()
    schedule = schedules.create(
        "thread", "__workspace__", "thread", kind="cadence", spec="60",
        max_runs=2, now="2026-01-01T00:00:00+00:00", db=db,
    )
    now = "2026-01-01T00:01:00+00:00"
    (request,) = ScheduleSource().due(now, db=db)
    with write_transaction(db, scope="test_schedule_settle") as connection:
        trigger, _created = triggers.enqueue(request, db=connection, now=now)
        ScheduleSource().settle(trigger, outcome, "test", db=connection)

    row = db.execute(
        "SELECT state,runs,next_run_at FROM automation_schedules WHERE id=?",
        (schedule.id,),
    ).fetchone()
    assert dict(row) == {
        "state": "active",
        "runs": 1,
        "next_run_at": "2026-01-01T00:02:00+00:00",
    }
