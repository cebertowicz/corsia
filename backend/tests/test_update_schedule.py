from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


def test_tonight_is_due_only_inside_its_one_hour_window():
    from automation.update_schedule import possible_trigger, should_drop_tonight

    now = datetime(2026, 10, 14, 3, 30, tzinfo=timezone.utc)
    schedule = {"automatic": False, "time": "03:00", "tonight_at": "2026-10-14T03:00:00+00:00"}

    assert possible_trigger(schedule, now=now, update_available=True, attempted_today=False) == "tonight"
    assert not should_drop_tonight(schedule, now=now)
    assert possible_trigger(schedule, now=now, update_available=False, attempted_today=False) is None
    assert should_drop_tonight(schedule, now=now + timedelta(minutes=30))


def test_tonight_always_uses_the_current_schedule_minute():
    from automation.update_schedule import possible_trigger

    schedule = {
        "automatic": False,
        "time": "04:00",
        "tonight_at": "2026-10-14T03:00:00+00:00",
    }
    at_old_minute = datetime(2026, 10, 14, 3, 10, tzinfo=timezone.utc)
    at_new_minute = datetime(2026, 10, 14, 4, 10, tzinfo=timezone.utc)

    assert possible_trigger(schedule, now=at_old_minute, update_available=True, attempted_today=False) is None
    assert possible_trigger(schedule, now=at_new_minute, update_available=True, attempted_today=False) == "tonight"


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

    updated = get_update_schedule()
    assert updated["automatic"] is True
    assert updated["time"] == "04:00"
    assert updated["tonight_at"] is not None


def test_automatic_runs_once_in_its_local_hour_after_no_update_attempt():
    from automation.update_schedule import possible_trigger

    schedule = {"automatic": True, "time": "03:00", "tonight_at": None}
    at_three = datetime(2026, 10, 14, 3, 45, tzinfo=timezone.utc)

    assert possible_trigger(schedule, now=at_three, update_available=True, attempted_today=False) == "automatic"
    assert possible_trigger(schedule, now=at_three, update_available=True, attempted_today=True) is None
    assert possible_trigger(schedule, now=at_three, update_available=False, attempted_today=False) is None
    assert possible_trigger(schedule, now=at_three.replace(hour=4), update_available=True, attempted_today=False) is None


def test_automatic_waits_for_the_exact_minute_it_shows():
    from automation.update_schedule import possible_trigger

    schedule = {"automatic": True, "time": "03:45", "tonight_at": None}
    at = datetime(2026, 10, 14, 3, 0, tzinfo=timezone.utc)

    assert possible_trigger(schedule, now=at, update_available=True, attempted_today=False) is None
    assert possible_trigger(schedule, now=at.replace(minute=45), update_available=True, attempted_today=False) == "automatic"
    assert possible_trigger(schedule, now=at.replace(hour=4, minute=30), update_available=True, attempted_today=False) == "automatic"
    assert possible_trigger(schedule, now=at.replace(hour=4, minute=45), update_available=True, attempted_today=False) is None


@pytest.mark.parametrize("old_time,new_time", [("22:00", "02:00"), ("23:30", "23:45")])
def test_editing_time_recomputes_a_pending_tonight_choice(isolated_db, old_time, new_time):
    from automation.update_schedule import update_schedule_patch
    from infra.settings import get_update_schedule

    now = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc)
    update_schedule_patch({"time": old_time, "tonight": True}, now=now, user_id="steve")
    update_schedule_patch({"time": new_time}, now=now, user_id="steve")

    pending = get_update_schedule()["tonight_at"]
    assert pending is not None
    assert datetime.fromisoformat(pending).astimezone(now.tzinfo).strftime("%H:%M") == new_time


def test_update_schedule_rejects_noncanonical_time_without_persisting(isolated_db):
    import asyncio

    from api.system_admin import update_schedule
    from automation.update_schedule import update_schedule_patch
    from infra.settings import get_update_schedule

    now = datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)
    update_schedule_patch({"time": "04:05"}, now=now, user_id="steve")

    with pytest.raises(Exception) as raised:
        asyncio.run(
            update_schedule(
                {"time": "1:000"},
                user={"id": "steve", "token_kind": "session"},
            )
        )

    assert getattr(raised.value, "status_code", None) == 400
    assert get_update_schedule()["time"] == "04:05"


def test_valid_schedule_time_round_trips_to_nightly_read(isolated_db):
    from automation.update_schedule import possible_trigger, update_schedule_patch
    from infra.settings import get_update_schedule

    now = datetime(2026, 10, 14, 4, 5, tzinfo=timezone.utc)
    update_schedule_patch({"automatic": True, "time": "04:05"}, now=now, user_id="steve")

    schedule = get_update_schedule(include_owner=True)
    assert schedule["time"] == "04:05"
    assert possible_trigger(schedule, now=now, update_available=True, attempted_today=False) == "automatic"


def test_turning_tonight_off_clears_the_pending_choice(isolated_db):
    from automation.update_schedule import update_schedule_patch
    from infra.settings import get_update_schedule

    now = datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)
    update_schedule_patch({"tonight": True}, now=now, user_id="steve")
    update_schedule_patch({"tonight": False}, now=now, user_id="steve")

    assert get_update_schedule()["tonight_at"] is None


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
    assert self_restart.abort_operation(failed, error="launch failed", failure_code="launch_failed", db=db)
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
    launch_environment = {}
    issue_token = accounts.issue_lifecycle_service_token

    def capture_token(user_id, operation_id):
        token = issue_token(user_id, operation_id)
        minted_tokens.append(token)
        return token

    class Process:
        def poll(self):
            return None

    def fake_popen(_command, *, stdout, **kwargs):
        launch_environment.update(kwargs["env"])
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
        scheduled_update.launch_scheduled_update(
            "automatic", "1.2.3", {"owner_id": "steve"}
        )
    )

    assert len(minted_tokens) == 1
    from api.scheduled_update import SCHEDULED_DRAIN_MAX_WAIT_SECONDS
    from infra.accounts import SERVICE_TTL

    assert launch_environment["CORSIA_DRAIN_MAX_WAIT"] == str(SCHEDULED_DRAIN_MAX_WAIT_SECONDS)
    assert SERVICE_TTL > timedelta(seconds=SCHEDULED_DRAIN_MAX_WAIT_SECONDS + 30)
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
    monkeypatch.setattr(update_schedule, "clear_tonight", lambda value: value)
    monkeypatch.setattr(scheduled_update, "launch_scheduled_update", launch)
    return schedule


def test_schedule_skips_remote_update_status_when_no_trigger_can_be_due(monkeypatch):
    import asyncio

    from infra import db_async, settings, update_status
    from jobs.nightly import _run_scheduled_update_if_due

    async def run_in_db(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(
        settings,
        "get_update_schedule",
        lambda **_kwargs: {"automatic": False, "time": "03:00", "tonight_at": None},
    )
    monkeypatch.setattr(db_async, "run_in_db", run_in_db)
    monkeypatch.setattr(update_status, "update_status", lambda _refresh: pytest.fail("must not fetch"))

    assert asyncio.run(_run_scheduled_update_if_due(datetime(2026, 10, 14, 1, tzinfo=timezone.utc))) is None


def test_due_tonight_without_an_available_update_preserves_the_choice(
    isolated_db, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    from api import scheduled_update
    from automation import update_schedule
    from infra import update_status
    from infra.settings import get_update_schedule
    from jobs.nightly import _run_scheduled_update_if_due

    scheduled_at = datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)
    now = scheduled_at.replace(hour=3, minute=10)
    update_schedule.update_schedule_patch(
        {"tonight": True}, now=scheduled_at, user_id="steve"
    )
    pending = get_update_schedule()["tonight_at"]

    monkeypatch.setattr(
        update_status,
        "update_status",
        lambda _refresh: SimpleNamespace(to_dict=lambda: {"available": False}),
    )

    async def launch(*_args):
        pytest.fail("must not launch without an available update")

    monkeypatch.setattr(scheduled_update, "launch_scheduled_update", launch)

    assert asyncio.run(_run_scheduled_update_if_due(now)) is None
    assert get_update_schedule()["tonight_at"] == pending


@pytest.mark.asyncio
async def test_false_tonight_claim_aborts_after_acquire_without_launch(isolated_db, monkeypatch):
    from pathlib import Path
    from unittest.mock import AsyncMock

    from api import settings_restart
    from infra.database import get_db

    launch = AsyncMock()
    monkeypatch.setattr(settings_restart, "_launch_settings_restart", launch)
    monkeypatch.setattr(settings_restart, "_check_update_backup_room", AsyncMock())
    monkeypatch.setattr(settings_restart, "_prepare_owned_release", AsyncMock())
    monkeypatch.setattr(settings_restart, "_begin_settings_drain", AsyncMock())

    assert await settings_restart.prelaunch_settings_restart(
        {"id": "steve"},
        "update",
        Path("/unused"),
        {},
        ["restart"],
        lambda name: Path(name),
        caller_token="human",
        claim_hook=lambda: False,
        trigger="tonight",
    ) is None

    connection = get_db()
    try:
        rows = [dict(row) for row in connection.execute(
            "SELECT status, phase FROM self_restart_requests"
        ).fetchall()]
    finally:
        connection.close()
    assert rows == [{"status": "failed", "phase": None}]
    launch.assert_not_awaited()


def test_refused_acquire_keeps_tonight_without_an_operation_row(isolated_db, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import settings_restart
    from automation import update_schedule
    from fastapi import HTTPException
    from infra import accounts, update_status
    from infra.database import get_db
    from infra.settings import get_update_schedule
    from jobs.nightly import _run_scheduled_update_if_due

    scheduled_at = datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)
    now = scheduled_at.replace(hour=3, minute=10)
    update_schedule.update_schedule_patch(
        {"tonight": True}, now=scheduled_at, user_id="steve"
    )
    pending = get_update_schedule()["tonight_at"]
    monkeypatch.setenv("BACKEND_PORT", "1957")
    monkeypatch.setattr(accounts, "get_user", lambda _user_id: {"id": "steve"})
    monkeypatch.setattr(
        update_status,
        "update_status",
        lambda _refresh: SimpleNamespace(to_dict=lambda: {"available": True, "target_version": "1.2.3"}),
    )
    monkeypatch.setattr(
        settings_restart,
        "_acquire_settings_restart",
        AsyncMock(side_effect=HTTPException(409, "A restart is already in flight.")),
    )

    assert asyncio.run(_run_scheduled_update_if_due(now)) is None
    assert get_update_schedule()["tonight_at"] == pending
    connection = get_db()
    try:
        assert connection.execute("SELECT COUNT(*) FROM self_restart_requests").fetchone()[0] == 0
    finally:
        connection.close()


def test_prepare_failure_after_tonight_claim_does_not_retry_in_the_same_window(
    isolated_db, monkeypatch
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import settings_restart
    from automation import update_schedule
    from fastapi import HTTPException
    from infra import accounts, update_status
    from infra.database import get_db
    from infra.settings import get_update_schedule
    from jobs.nightly import _run_scheduled_update_if_due

    scheduled_at = datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)
    now = scheduled_at.replace(hour=3, minute=10)
    update_schedule.update_schedule_patch(
        {"tonight": True}, now=scheduled_at, user_id="steve"
    )
    monkeypatch.setenv("BACKEND_PORT", "1957")
    monkeypatch.setattr(accounts, "get_user", lambda _user_id: {"id": "steve"})
    monkeypatch.setattr(
        update_status,
        "update_status",
        lambda _refresh: SimpleNamespace(to_dict=lambda: {"available": True, "target_version": "1.2.3"}),
    )
    monkeypatch.setattr(settings_restart, "_check_update_backup_room", AsyncMock())

    def fail_prepare(*_args):
        failure = HTTPException(409, "Update preparation failed")
        setattr(failure, "update_failure_code", "build_failed")
        raise failure

    monkeypatch.setattr(settings_restart, "_prepare_current_release", fail_prepare)

    assert asyncio.run(_run_scheduled_update_if_due(now)) is None
    assert get_update_schedule()["tonight_at"] is None
    connection = get_db()
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT status, failure_code FROM self_restart_requests"
            ).fetchall()
        ]
    finally:
        connection.close()
    assert rows == [{"status": "failed", "failure_code": "build_failed"}]
    assert asyncio.run(_run_scheduled_update_if_due(now + timedelta(minutes=5))) is None
    connection = get_db()
    try:
        assert connection.execute("SELECT COUNT(*) FROM self_restart_requests").fetchone()[0] == 1
    finally:
        connection.close()


def test_nightly_delegates_the_schedule_snapshot_to_its_only_launch_entry(monkeypatch):
    import asyncio

    from jobs.nightly import _run_scheduled_update_if_due

    launched = []

    async def launch(trigger, target_version, schedule):
        launched.append((trigger, target_version, schedule))
        return "c" * 32

    schedule = _patch_scheduled_check(monkeypatch, launch)
    now = datetime(2026, 10, 14, 3, 10, tzinfo=timezone.utc)

    assert asyncio.run(_run_scheduled_update_if_due(now)) == "c" * 32
    assert launched == [("tonight", "1.2.3", schedule)]


def test_scheduled_launch_failure_is_logged_not_swallowed(monkeypatch):
    import asyncio

    from loguru import logger

    from jobs.nightly import _run_scheduled_update_if_due

    async def launch(_trigger, _target_version, _schedule):
        raise RuntimeError("scheduled update cannot launch: BACKEND_PORT is not a port number")

    _patch_scheduled_check(monkeypatch, launch)
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), level="ERROR")
    try:
        now = datetime(2026, 10, 14, 3, 10, tzinfo=timezone.utc)
        assert asyncio.run(_run_scheduled_update_if_due(now)) is None
    finally:
        logger.remove(sink)

    assert any("BACKEND_PORT is not a port number" in message for message in messages)
