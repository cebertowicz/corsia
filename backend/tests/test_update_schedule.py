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


def test_tonight_always_uses_the_current_schedule_minute():
    from automation.update_schedule import due_trigger

    schedule = {
        "automatic": False,
        "time": "04:00",
        "tonight_at": "2026-10-14T03:00:00+00:00",
    }
    at_old_minute = datetime(2026, 10, 14, 3, 10, tzinfo=timezone.utc)
    at_new_minute = datetime(2026, 10, 14, 4, 10, tzinfo=timezone.utc)

    assert due_trigger(schedule, now=at_old_minute, update_available=True, attempted_today=False) is None
    assert due_trigger(schedule, now=at_new_minute, update_available=True, attempted_today=False) == "tonight"


def test_clear_tonight_keeps_a_patch_written_after_the_tick_read(isolated_db):
    from automation.update_schedule import clear_tonight, update_schedule_patch
    from infra.settings import get_update_schedule

    now = datetime(2026, 10, 14, 2, 30, tzinfo=timezone.utc)
    update_schedule_patch({"tonight": True}, now=now, user_id="steve")
    tick_schedule = get_update_schedule(include_owner=True)
    update_schedule_patch(
        {"automatic": True, "time": "04:00"}, now=now, user_id="steve"
    )

    clear_tonight(tick_schedule)

    assert get_update_schedule() == {
        "automatic": True,
        "time": "04:00",
        "tonight_at": None,
    }


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
    assert "callback_credential_hash" not in columns
    assert db.execute("SELECT status FROM self_restart_requests WHERE id='legacy'").fetchone()[0] == "complete"
    indexes = {row[1] for row in db.execute("PRAGMA index_list(self_restart_requests)")}
    assert "idx_self_restart_requests_update_attempts" in indexes


def _operation_table() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE self_restart_requests ("
        "id TEXT PRIMARY KEY, requester_thread_id TEXT, requester_user TEXT, from_sha TEXT, "
        "branch TEXT, reason TEXT, kind TEXT, trigger TEXT, target_version TEXT, "
        "changes TEXT, outcome_seen_at TEXT, status TEXT, phase TEXT, requested_at TEXT, "
        "cutoff_at TEXT, updated_at TEXT, settled_at TEXT, deployed_sha TEXT, error TEXT, "
        "failure_code TEXT, failure_detail TEXT)"
    )
    db.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
    db.execute(
        "CREATE TABLE auth_tokens (token_hash TEXT PRIMARY KEY, user_id TEXT, kind TEXT, "
        "label TEXT, created_at TEXT, expires_at TEXT)"
    )
    db.execute("INSERT INTO users (id) VALUES ('steve')")
    return db


def test_attempted_today_uses_local_update_acquisition_date():
    db = _operation_table()
    local_now = datetime(2026, 10, 14, 3, 15, tzinfo=timezone.utc)
    db.execute(
        "INSERT INTO self_restart_requests (id,kind,status,requested_at) VALUES (?, 'update', 'complete', ?)",
        ("b" * 32, (local_now - timedelta(minutes=30)).isoformat()),
    )
    db.commit()

    from automation.update_schedule import update_attempted_today

    assert update_attempted_today(now=local_now, db=db)
    assert not update_attempted_today(now=local_now + timedelta(days=1), db=db)


def test_lifecycle_service_tokens_are_revoked_at_every_terminal_path():
    from automation import self_restart
    from infra import accounts

    db = _operation_table()

    def acquire() -> tuple[str, str]:
        operation_id = self_restart.try_acquire(
            None, "steve", None, None, "update", kind="update", db=db
        )
        assert operation_id is not None
        return operation_id, accounts.issue_lifecycle_service_token("steve", operation_id)

    completed, completed_token = acquire()
    assert self_restart.transition_operation(completed, expected="holding", target="resuming", db=db)
    assert self_restart.complete_operation(completed, db=db)
    assert accounts.resolve_token(completed_token, db=db) is None

    failed, failed_token = acquire()
    assert self_restart.abort_operation(failed, error="expired", failure_code="credential_expired", db=db)
    assert accounts.resolve_token(failed_token, db=db) is None

    cancelled, cancelled_token = acquire()
    assert self_restart.abort_operation(cancelled, error="cancelled", cancelled=True, db=db)
    assert accounts.resolve_token(cancelled_token, db=db) is None

    abandoned, abandoned_token = acquire()
    db.execute(
        "UPDATE self_restart_requests SET phase=NULL, requested_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), abandoned),
    )
    db.commit()
    assert self_restart.clear_stale_in_flight(db=db) == 1
    assert accounts.resolve_token(abandoned_token, db=db) is None
    stored = db.execute("SELECT * FROM self_restart_requests WHERE id=?", (abandoned,)).fetchone()
    assert abandoned_token not in " ".join(str(value) for value in stored)


def test_scheduled_launch_keeps_lifecycle_token_out_of_observable_output(
    isolated_db, monkeypatch, tmp_path
):
    import asyncio
    from unittest.mock import AsyncMock

    from api import restart_shell, scheduled_update, settings_restart, system_admin
    from infra import accounts, product_events
    from infra.database import get_db

    restart_log = tmp_path / "restart.log"
    emitted_events = []
    minted_tokens = []
    issue_token = accounts.issue_lifecycle_service_token

    def capture_token(user_id, operation_id):
        token = issue_token(user_id, operation_id)
        minted_tokens.append(token)
        return token

    class Process:
        def poll(self):
            return None

    def fake_popen(_command, *, stdout, **_kwargs):
        stdout.write("detached lifecycle started\n")
        stdout.flush()
        return Process()

    monkeypatch.setenv("BACKEND_PORT", "1957")
    monkeypatch.setattr(accounts, "issue_lifecycle_service_token", capture_token)
    monkeypatch.setattr(
        product_events,
        "emit_product_event",
        lambda *args, **kwargs: emitted_events.append((args, kwargs)),
    )
    monkeypatch.setattr(settings_restart, "_check_update_backup_room", AsyncMock())
    monkeypatch.setattr(settings_restart, "_prepare_owned_release", AsyncMock())
    monkeypatch.setattr(settings_restart, "_begin_settings_drain", AsyncMock())
    monkeypatch.setattr(system_admin, "_restart_log_path", lambda _name: restart_log)
    monkeypatch.setattr(restart_shell.subprocess, "Popen", fake_popen)

    operation_id = asyncio.run(
        scheduled_update.launch_scheduled_update("automatic", "1.2.3", "steve")
    )

    assert len(minted_tokens) == 1
    token = minted_tokens[0]
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT * FROM self_restart_requests WHERE id=?", (operation_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert token not in " ".join(str(value) for value in row)
    assert token not in repr(emitted_events)
    assert token.encode() not in restart_log.read_bytes()


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
    from automation import update_schedule
    from infra import db_async, settings, update_status

    cleared = []
    schedule = {
        "automatic": False,
        "time": "03:00",
        "tonight_at": "2026-10-14T03:00:00+00:00",
        "owner_id": "steve",
    }

    async def run_in_db(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(settings, "get_update_schedule", lambda **_kwargs: schedule)
    monkeypatch.setattr(
        update_status,
        "update_status",
        lambda _refresh: SimpleNamespace(to_dict=lambda: {"available": True, "target_version": "1.2.3"}),
    )
    monkeypatch.setattr(db_async, "run_in_db", run_in_db)
    monkeypatch.setattr(update_schedule, "update_attempted_today", lambda **_kwargs: False)
    monkeypatch.setattr(update_schedule, "clear_tonight", lambda value: cleared.append(value) or value)
    monkeypatch.setattr(scheduled_update, "launch_scheduled_update", launch)
    return cleared


def test_tonight_is_claimed_once_and_launches_its_update(monkeypatch):
    import asyncio

    from jobs.nightly import _run_scheduled_update_if_due

    launched = []

    async def launch(trigger, target_version, user_id):
        launched.append((trigger, target_version, user_id))
        return "c" * 32

    cleared = _patch_scheduled_check(monkeypatch, launch)
    now = datetime(2026, 10, 14, 3, 10, tzinfo=timezone.utc)

    assert asyncio.run(_run_scheduled_update_if_due(now)) == "c" * 32
    assert launched == [("tonight", "1.2.3", "steve")]
    assert len(cleared) == 1


def test_scheduled_launch_failure_is_logged_not_swallowed(monkeypatch):
    import asyncio

    from loguru import logger

    from jobs.nightly import _run_scheduled_update_if_due

    async def launch(_trigger, _target_version, _user_id):
        raise RuntimeError("scheduled update cannot launch: BACKEND_PORT is not a port number")

    cleared = _patch_scheduled_check(monkeypatch, launch)
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), level="ERROR")
    try:
        now = datetime(2026, 10, 14, 3, 10, tzinfo=timezone.utc)
        assert asyncio.run(_run_scheduled_update_if_due(now)) is None
    finally:
        logger.remove(sink)

    assert len(cleared) == 0
    assert any("BACKEND_PORT is not a port number" in message for message in messages)
