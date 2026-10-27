"""Automation waits for archived owners and reports its final cadence run."""

from automation import schedules, script_runner
from automation.continuation import execution, runner
from automation.continuation.sources.schedules import ScheduleSource
from tests.test_continuation_schedules import _db, _delivered
from tests.test_script_schedules import _register

import pytest

T0 = "2026-01-01T00:00:00+00:00"


def test_final_cadence_wake_and_clock():
    db = _db()
    cadence = schedules.create("thread", "__workspace__", "thread", kind="cadence", spec="60",
                               max_runs=2, payload={"summary": "Custom."}, now=T0, db=db)
    clock = schedules.create("thread", "__workspace__", "thread", kind="clock",
                             spec="2026-01-01T00:01:00+00:00", max_runs=1, now=T0, db=db)
    first = {r.subject_id: r for r in ScheduleSource().due("2026-01-01T00:01:00+00:00", db=db)}
    assert "final_run" not in first[cadence.id].details
    assert "final_run" not in first[clock.id].details
    schedules.settle_run(cadence.id, "2026-01-01T00:01:00+00:00", db=db)
    final = next(r for r in ScheduleSource().due("2026-01-01T00:02:00+00:00", db=db)
                 if r.subject_id == cadence.id)
    _, summary, details = execution.event_for(final)
    assert summary == ("Custom. This is the last of this schedule's 2 runs; it stops after this one. "
                       "To keep it going, arm a new one with automation_schedule.")
    assert (details["final_run"], details["runs"], details["max_runs"]) == (True, 1, 2)


@pytest.mark.asyncio
async def test_archived_cadence_waits_and_resumes_without_burst():
    db = _db()
    schedule = schedules.create("thread", "__workspace__", "thread", kind="cadence", spec="60",
                                max_runs=20, now=T0, db=db)
    db.execute("UPDATE threads SET lifecycle_state='archived', archived_reason='user' WHERE id='thread'")
    db.commit()
    now = "2026-01-01T00:11:00+00:00"
    assert ScheduleSource().due(now, db=db) == []
    assert schedules.get(schedule.id, db=db).runs == 0
    db.execute("UPDATE threads SET lifecycle_state='active', archived_reason=NULL WHERE id='thread'")
    db.commit()
    result = await runner.sweep(now, db=db, deliver=_delivered)
    assert result["woken"] == ["thread"]
    row = schedules.get(schedule.id, db=db)
    assert (row.runs, row.next_run_at) == (1, "2026-01-01T00:12:00+00:00")
    assert ScheduleSource().due(now, db=db) == []


def test_archived_script_is_held_without_report(tmp_path):
    from tests.test_script_schedules import _db as script_db

    db = script_db()
    schedule = _register(db, tmp_path, now=T0)
    db.execute("UPDATE threads SET lifecycle_state='archived', archived_reason='user' WHERE id='owner'")
    db.commit()
    assert script_runner.claim_due("2026-01-01T00:10:00+00:00", 1, set(), db) == []
    assert schedules.get(schedule.id, db=db).state == "active"
    assert db.execute("SELECT count(*) FROM dead_letter_events").fetchone()[0] == 0


def test_purged_script_cancels_and_reports_once(tmp_path):
    from tests.test_script_schedules import _db as script_db

    db = script_db()
    schedule = _register(db, tmp_path, now=T0)
    db.execute("INSERT INTO threads (id,user_id,created_at,updated_at) VALUES ('parent','user','t','t')")
    db.execute("UPDATE threads SET lifecycle_state='purged', parent_thread_id='parent' WHERE id='owner'")
    db.commit()
    now = "2026-01-01T00:10:00+00:00"
    assert script_runner.claim_due(now, 1, set(), db) == []
    assert script_runner.claim_due(now, 1, set(), db) == []
    row = schedules.get(schedule.id, db=db)
    assert (row.state, row.last_status) == ("cancelled", "owner_gone")
    assert db.execute("SELECT count(*) FROM messages WHERE thread_id='parent' AND content LIKE '[EVENT: script.owner_gone%'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_purged_cadence_fails_once_and_drives_report(monkeypatch):
    from unittest.mock import AsyncMock

    db = _db()
    schedule = schedules.create("thread", "__workspace__", "thread", kind="cadence", spec="60",
                                max_runs=10, now=T0, db=db)
    db.execute("INSERT INTO threads (id,user_id,created_at,updated_at) VALUES ('parent','user','t','t')")
    db.execute("UPDATE threads SET lifecycle_state='purged', parent_thread_id='parent' WHERE id='thread'")
    db.commit()
    drive = AsyncMock()
    monkeypatch.setattr("infra.thread_execution.drive_thread", drive)
    monkeypatch.setattr("infra.failed_wake_surface._owners_holding_reports", lambda ids: [
        row[0] for row in db.execute(
            "SELECT DISTINCT thread_id FROM messages WHERE delivery_state='queued' AND id>=? AND id<?",
            (f"wake-failed:{ids[0]}:", f"wake-failed:{ids[0]};"),
        )
    ])

    async def in_db(fn, *args):
        return fn(*args)

    monkeypatch.setattr("infra.db_async.run_in_db", in_db)
    async def fail(*_args):
        return False
    result = await runner.sweep("2026-01-01T00:01:00+00:00", db=db, deliver=fail)
    assert result["errored"] == ["thread"]
    drive.assert_awaited_once_with("parent")
    assert db.execute("SELECT count(*) FROM messages WHERE thread_id='parent' AND id LIKE 'wake-failed:%'").fetchone()[0] == 1
    row = schedules.get(schedule.id, db=db)
    assert (row.state, row.last_status, row.runs) == ("cancelled", "owner_gone", 0)
    assert ScheduleSource().due("2026-01-01T00:02:00+00:00", db=db) == []


@pytest.mark.parametrize("state,reason", [("dormant", None), ("archived", "dormancy")])
def test_dormancy_does_not_hold_automation(state, reason):
    db = _db()
    schedules.create("thread", "__workspace__", "thread", kind="cadence", spec="60",
                     max_runs=2, now=T0, db=db)
    db.execute("UPDATE threads SET lifecycle_state=?, archived_reason=? WHERE id='thread'", (state, reason))
    assert len(ScheduleSource().due("2026-01-01T00:01:00+00:00", db=db)) == 1
