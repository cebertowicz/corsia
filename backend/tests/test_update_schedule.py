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
    assert {"callback_credential_hash", "overnight_outcome_claimed_at"} <= columns
    assert db.execute("SELECT status FROM self_restart_requests WHERE id='legacy'").fetchone()[0] == "complete"


def _operation_table() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE self_restart_requests ("
        "id TEXT PRIMARY KEY, requester_thread_id TEXT, requester_user TEXT, from_sha TEXT, "
        "branch TEXT, reason TEXT, kind TEXT, trigger TEXT, target_version TEXT, "
        "callback_credential_hash TEXT, status TEXT, phase TEXT, requested_at TEXT, "
        "cutoff_at TEXT, updated_at TEXT, settled_at TEXT, deployed_sha TEXT, error TEXT, "
        "failure_code TEXT, failure_detail TEXT, overnight_outcome_claimed_at TEXT)"
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


def test_overnight_outcome_is_claimed_once_across_devices():
    from automation import self_restart

    db = _operation_table()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO self_restart_requests (id,kind,trigger,status,requested_at,updated_at) "
        "VALUES (?, 'update', 'automatic', 'complete', ?, ?)",
        ("a" * 32, now, now),
    )
    db.commit()

    assert self_restart.claim_overnight_outcome("a" * 32, db=db)
    assert not self_restart.claim_overnight_outcome("a" * 32, db=db)
