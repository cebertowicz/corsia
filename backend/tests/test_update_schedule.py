from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


def test_tonight_is_due_only_inside_its_one_hour_window():
    from automation.update_schedule import due_trigger, should_drop_tonight

    now = datetime(2026, 10, 14, 3, 30, tzinfo=timezone.utc)
    schedule = {"automatic": False, "time": "03:00", "tonight_at": "2026-10-14T03:00:00+00:00"}

    assert due_trigger(schedule, now=now, update_available=True, attempted_today=False) == "tonight"
    assert not should_drop_tonight(schedule, now=now)
    assert due_trigger(schedule, now=now, update_available=False, attempted_today=False) is None
    assert should_drop_tonight(schedule, now=now + timedelta(minutes=30))


def test_automatic_runs_once_in_its_local_hour_after_no_update_attempt():
    from automation.update_schedule import due_trigger

    schedule = {"automatic": True, "time": "03:00", "tonight_at": None}
    at_three = datetime(2026, 10, 14, 3, 45, tzinfo=timezone.utc)

    assert due_trigger(schedule, now=at_three, update_available=True, attempted_today=False) == "automatic"
    assert due_trigger(schedule, now=at_three, update_available=True, attempted_today=True) is None
    assert due_trigger(schedule, now=at_three, update_available=False, attempted_today=False) is None
    assert due_trigger(schedule, now=at_three.replace(hour=4), update_available=True, attempted_today=False) is None


def test_automatic_waits_for_the_exact_minute_it_shows():
    from automation.update_schedule import due_trigger

    schedule = {"automatic": True, "time": "03:45", "tonight_at": None}
    at = datetime(2026, 10, 14, 3, 0, tzinfo=timezone.utc)

    assert due_trigger(schedule, now=at, update_available=True, attempted_today=False) is None
    assert due_trigger(schedule, now=at.replace(minute=45), update_available=True, attempted_today=False) == "automatic"
    assert due_trigger(schedule, now=at.replace(hour=4, minute=30), update_available=True, attempted_today=False) == "automatic"
    assert due_trigger(schedule, now=at.replace(hour=4, minute=45), update_available=True, attempted_today=False) is None


def test_scheduled_update_migration_preserves_existing_operation_rows():
    from infra.migration_114_to_115_scheduled_updates import migrate_114_to_115

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE self_restart_requests (id TEXT PRIMARY KEY, status TEXT, kind TEXT, requested_at TEXT)"
    )
    db.execute(
        "INSERT INTO self_restart_requests VALUES ('legacy', 'complete', 'update', '2026-10-14T03:00:00+00:00')"
    )

    migrate_114_to_115(db)

    columns = {row[1] for row in db.execute("PRAGMA table_info(self_restart_requests)")}
    assert "callback_credential_hash" in columns
    assert db.execute("SELECT status FROM self_restart_requests WHERE id='legacy'").fetchone()[0] == "complete"


def _operation_table() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE self_restart_requests ("
        "id TEXT PRIMARY KEY, requester_thread_id TEXT, requester_user TEXT, from_sha TEXT, "
        "branch TEXT, reason TEXT, kind TEXT, trigger TEXT, target_version TEXT, "
        "callback_credential_hash TEXT, changes TEXT, outcome_seen_at TEXT, status TEXT, phase TEXT, requested_at TEXT, "
        "cutoff_at TEXT, updated_at TEXT, settled_at TEXT, deployed_sha TEXT, error TEXT, "
        "failure_code TEXT, failure_detail TEXT)"
    )
    return db


def test_attempted_today_uses_local_update_acquisition_date():
    from automation import self_restart

    db = _operation_table()
    local_now = datetime(2026, 10, 14, 3, 15, tzinfo=timezone.utc)
    db.execute(
        "INSERT INTO self_restart_requests (id,kind,status,requested_at) VALUES (?, 'update', 'complete', ?)",
        ("b" * 32, (local_now - timedelta(minutes=30)).isoformat()),
    )
    db.commit()

    assert self_restart.update_attempted_today(now=local_now, db=db)
    assert not self_restart.update_attempted_today(now=local_now + timedelta(days=1), db=db)


def test_callback_credential_is_operation_scoped_and_dead_at_terminal_state():
    from automation import self_restart

    db = _operation_table()
    credential = self_restart.mint_callback_credential()
    first = self_restart.try_acquire(
        None, None, None, None, "update", kind="update", trigger="tonight",
        callback_hash=self_restart.callback_credential_hash(credential), db=db,
    )
    assert first is not None
    assert self_restart.callback_credential_matches(first, credential, db=db)
    assert not self_restart.callback_credential_matches("f" * 32, credential, db=db)

    assert self_restart.abort_operation(first, error="cancelled", cancelled=True, db=db)
    assert not self_restart.callback_credential_matches(first, credential, db=db)

    second_credential = self_restart.mint_callback_credential()
    second = self_restart.try_acquire(
        None, None, None, None, "update", kind="update", trigger="automatic",
        callback_hash=self_restart.callback_credential_hash(second_credential), db=db,
    )
    assert second is not None
    assert not self_restart.callback_credential_matches(second, credential, db=db)
    assert self_restart.callback_credential_matches(second, second_credential, db=db)


def test_scheduled_outcome_is_marked_once_across_devices():
    from automation.update_outcome import mark_outcome_seen

    db = _operation_table()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO self_restart_requests (id,kind,trigger,status,requested_at,updated_at) "
        "VALUES (?, 'update', 'automatic', 'complete', ?, ?)",
        ("a" * 32, now, now),
    )
    db.commit()

    assert mark_outcome_seen("a" * 32, db=db) is True
    assert mark_outcome_seen("a" * 32, db=db) is True


def _patch_scheduled_check(monkeypatch, launch):
    from types import SimpleNamespace

    from api import scheduled_update
    from automation import self_restart, update_schedule
    from infra import db_async, settings, update_status

    cleared = []
    schedule = {"automatic": False, "time": "03:00", "tonight_at": "2026-10-14T03:00:00+00:00"}

    async def run_in_db(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(settings, "get_update_schedule", lambda: schedule)
    monkeypatch.setattr(
        update_status,
        "update_status",
        lambda _refresh: SimpleNamespace(to_dict=lambda: {"available": True, "target_version": "1.2.3"}),
    )
    monkeypatch.setattr(db_async, "run_in_db", run_in_db)
    monkeypatch.setattr(self_restart, "update_attempted_today", lambda **_kwargs: False)
    monkeypatch.setattr(update_schedule, "clear_tonight", lambda value: cleared.append(value) or value)
    monkeypatch.setattr(scheduled_update, "launch_scheduled_update", launch)
    return cleared


def test_tonight_is_claimed_once_and_launches_its_update(monkeypatch):
    import asyncio

    from jobs.nightly import _run_scheduled_update_if_due

    launched = []

    async def launch(trigger, target_version):
        launched.append((trigger, target_version))
        return "c" * 32

    cleared = _patch_scheduled_check(monkeypatch, launch)
    now = datetime(2026, 10, 14, 3, 10, tzinfo=timezone.utc)

    assert asyncio.run(_run_scheduled_update_if_due(now)) == "c" * 32
    assert launched == [("tonight", "1.2.3")]
    assert len(cleared) == 1


def test_scheduled_launch_failure_is_logged_not_swallowed(monkeypatch):
    import asyncio

    from loguru import logger

    from jobs.nightly import _run_scheduled_update_if_due

    async def launch(_trigger, _target_version):
        raise RuntimeError("scheduled update cannot launch: BACKEND_PORT is not a port number")

    cleared = _patch_scheduled_check(monkeypatch, launch)
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), level="ERROR")
    try:
        now = datetime(2026, 10, 14, 3, 10, tzinfo=timezone.utc)
        assert asyncio.run(_run_scheduled_update_if_due(now)) is None
    finally:
        logger.remove(sink)

    assert len(cleared) == 1
    assert any("BACKEND_PORT is not a port number" in message for message in messages)
