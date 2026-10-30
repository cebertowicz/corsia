"""S1 durable Update failure causes."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from infra.account_validation import AccountError


@pytest.mark.asyncio
async def test_update_disk_refusal_persists_numbers_before_any_drain(isolated_db, monkeypatch):
    """A prepared update with no backup room releases its hold before steering."""
    from api import settings_restart
    from infra.database import get_db
    from infra.database_migrations import BackupRoomError

    prepare = AsyncMock()
    monkeypatch.setattr(settings_restart, "_prepare_owned_release", prepare)
    monkeypatch.setattr(
        settings_restart,
        "_check_update_backup_room",
        AsyncMock(side_effect=BackupRoomError("not enough room", 13, 8)),
    )
    drain = AsyncMock()
    monkeypatch.setattr(settings_restart, "_begin_settings_drain", drain)

    with pytest.raises(HTTPException) as raised:
        await settings_restart.prelaunch_settings_restart(
            {"id": "steve"}, "update", Path("/unused"), {}, ["restart"],
            lambda name: Path(name), caller_token="test-token",
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == {
        "code": "disk_space", "needed_bytes": 13, "free_bytes": 8,
    }
    connection = get_db()
    try:
        row = dict(connection.execute(
            "SELECT status,phase,kind,trigger,failure_code,failure_detail "
            "FROM self_restart_requests"
        ).fetchone())
    finally:
        connection.close()
    assert row == {
        "status": "failed", "phase": None, "kind": "update", "trigger": "manual",
        "failure_code": "disk_space",
        "failure_detail": '{"needed_bytes": 13, "free_bytes": 8}',
    }
    prepare.assert_not_awaited()
    drain.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raised_error", [OSError("stat failed"), asyncio.CancelledError()])
async def test_update_room_check_failure_leaves_no_in_flight_operation(
    isolated_db, monkeypatch, raised_error
):
    """Any failure of the room check, cancellation included, releases the hold."""
    from api import settings_restart
    from infra.database import get_db

    prepare = AsyncMock()
    monkeypatch.setattr(settings_restart, "_prepare_owned_release", prepare)
    monkeypatch.setattr(
        settings_restart, "_check_update_backup_room", AsyncMock(side_effect=raised_error)
    )
    drain = AsyncMock()
    monkeypatch.setattr(settings_restart, "_begin_settings_drain", drain)

    with pytest.raises(type(raised_error)):
        await settings_restart.prelaunch_settings_restart(
            {"id": "steve"}, "update", Path("/unused"), {}, ["restart"],
            lambda name: Path(name), caller_token="test-token",
        )

    connection = get_db()
    try:
        rows = [dict(row) for row in connection.execute(
            "SELECT status,phase FROM self_restart_requests"
        ).fetchall()]
    finally:
        connection.close()
    assert rows
    assert all(row["status"] != "in_flight" for row in rows)
    prepare.assert_not_awaited()
    drain.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised_error",
    [sqlite3.OperationalError("token store unavailable"), AccountError("account unavailable")],
)
async def test_lifecycle_token_mint_failure_releases_the_prepared_update_hold(
    isolated_db, monkeypatch, raised_error
):
    from api import settings_restart
    from infra import accounts, token_admin
    from infra.database import get_db

    monkeypatch.setattr(settings_restart, "_check_update_backup_room", AsyncMock())
    monkeypatch.setattr(settings_restart, "_prepare_owned_release", AsyncMock())
    drain = AsyncMock()
    monkeypatch.setattr(settings_restart, "_begin_settings_drain", drain)

    def mint(_user_id: str, _operation_id: str) -> str:
        raise raised_error

    monkeypatch.setattr(token_admin, "issue_lifecycle_service_token", mint)

    with pytest.raises(type(raised_error)):
        await settings_restart.prelaunch_settings_restart(
            {"id": "steve"}, "update", Path("/unused"), {}, ["restart"],
            lambda name: Path(name), caller_token=None,
        )

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
    assert rows == [{"status": "failed", "failure_code": None}]
    drain.assert_not_awaited()


def test_update_precheck_credits_prunable_backups(tmp_path, monkeypatch):
    """The preflight accepts room the migration will make by pruning stale backups."""
    from api import update_preflight
    from infra import database, database_migrations

    source = tmp_path / "corsia.db"
    source.write_bytes(b"database")
    stale = tmp_path / "corsia.db.pre-migration-v1-20260101T000000Z.bak"
    newest = tmp_path / "corsia.db.pre-migration-v1-20260201T000000Z.bak"
    stale.write_bytes(b"stale")
    newest.write_bytes(b"newest")
    monkeypatch.setattr(database_migrations, "_BACKUP_FREE_FLOOR_BYTES", 0)
    monkeypatch.setattr(database_migrations, "_BACKUP_MIN_FREE_RATIO", 1)
    monkeypatch.setattr(
        database_migrations.shutil, "disk_usage", lambda _path: type("Usage", (), {"free": 3})()
    )

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, sql):
            return Result((0, "main", str(source)) if "database_list" in sql else (1,))

    monkeypatch.setattr(database, "get_db", Connection)
    update_preflight._check_update_backup_room()
    stale.unlink()
    with pytest.raises(database_migrations.BackupRoomError) as refused:
        update_preflight._check_update_backup_room()
    assert refused.value.free_bytes == 3


@pytest.mark.asyncio
async def test_failed_update_marks_conductor_only_after_successful_send(isolated_db, monkeypatch):
    """A delivery receipt, not the attempt, is the conductor-notification marker."""
    from automation import self_restart
    from automation.update_failure_notice import tell_conductor_about_failed_update
    from core import agent_roster_queries
    from events import delivery
    from infra.database import get_db

    operation_id = self_restart.try_acquire(
        None, "steve", None, None, "update", kind="update", trigger="manual"
    )
    assert operation_id is not None
    assert self_restart.abort_operation(
        operation_id, error="build summary", failure_code="build_failed"
    )
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(
        agent_roster_queries, "corsia_agent_contact", lambda _user, _db: ({"id": "conductor"}, None)
    )
    monkeypatch.setattr(delivery, "deliver_to_thread", sent)

    await tell_conductor_about_failed_update(operation_id)
    await tell_conductor_about_failed_update(operation_id)

    connection = get_db()
    try:
        told_at = connection.execute(
            "SELECT conductor_told_at FROM self_restart_requests WHERE id=?", (operation_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert told_at is not None
    sent.assert_awaited_once()


@pytest.mark.asyncio
async def test_rolled_back_update_notice_names_the_target_and_running_versions(
    isolated_db, monkeypatch
):
    from automation import self_restart
    from automation.update_failure_notice import tell_conductor_about_failed_update
    from core import agent_roster_queries
    from events import delivery

    operation_id = self_restart.try_acquire(
        None, "steve", None, None, "update", kind="update", trigger="manual",
        previous_version="0.4.1",
    )
    assert operation_id is not None
    for expected, target in (
        ("holding", "steering"),
        ("steering", "draining"),
        ("draining", "ready"),
        ("ready", "restarting"),
        ("restarting", "resuming"),
    ):
        assert self_restart.transition_operation(
            operation_id, expected=expected, target=target,
        )
    assert self_restart.abort_operation(
        operation_id,
        error="Update target version 0.4.2 is not running (running: 0.4.1).",
        failure_code="rolled_back",
        failure_detail=json.dumps({
            "reason": "mismatch",
            "target_version": "0.4.2",
            "running_version": "0.4.1",
        }),
    )
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(
        agent_roster_queries, "corsia_agent_contact", lambda _user, _db: ({"id": "conductor"}, None)
    )
    monkeypatch.setattr(delivery, "deliver_to_thread", sent)

    await tell_conductor_about_failed_update(operation_id)

    assert "target version 0.4.2" in sent.await_args.args[2]
    assert "running version 0.4.1" in sent.await_args.args[2]
    assert "previous version 0.4.1" in sent.await_args.args[2]


@pytest.mark.asyncio
async def test_failed_update_keeps_conductor_marker_null_when_send_fails(isolated_db, monkeypatch):
    """An unreachable conductor can be retried later without claiming notification."""
    from automation import self_restart
    from automation.update_failure_notice import tell_conductor_about_failed_update
    from core import agent_roster_queries
    from events import delivery
    from infra.database import get_db

    operation_id = self_restart.try_acquire(
        None, "steve", None, None, "update", kind="update", trigger="manual"
    )
    assert operation_id is not None
    assert self_restart.abort_operation(operation_id, error="fetch summary", failure_code="fetch_failed")
    monkeypatch.setattr(
        agent_roster_queries, "corsia_agent_contact", lambda _user, _db: ({"id": "conductor"}, None)
    )
    monkeypatch.setattr(delivery, "deliver_to_thread", AsyncMock(return_value=False))

    await tell_conductor_about_failed_update(operation_id)

    connection = get_db()
    try:
        told_at = connection.execute(
            "SELECT conductor_told_at FROM self_restart_requests WHERE id=?", (operation_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert told_at is None


@pytest.mark.asyncio
async def test_classifier_failure_notice_uses_one_stable_delivery_key_per_version_and_reason(monkeypatch):
    from automation import update_failure_notice
    from jobs import corrections_recording

    delivered = AsyncMock()
    monkeypatch.setattr(corrections_recording, "owner_user_ids", lambda: ["owner"])
    monkeypatch.setattr(update_failure_notice, "_tell_conductor_about_owner_notice", delivered)

    await update_failure_notice.tell_conductor_about_corrections_classifier_failure(
        "v2", "load error:AssertionError", "Retry setup.",
    )
    await update_failure_notice.tell_conductor_about_corrections_classifier_failure(
        "v2", "load error:AssertionError", "Retry setup.",
    )

    first, second = delivered.await_args_list
    assert first.args == second.args == ("owner", "Corrections classifier v2 cannot run: Retry setup.")
    assert first.kwargs["event_type"] == "system.corrections_classifier_failed"
    assert first.kwargs["notice_name"] == "corrections classifier failure"
    assert first.kwargs["delivery_id"] == second.kwargs["delivery_id"]
    assert first.kwargs["delivery_id"].startswith("corrections-classifier-failed:v2:")


@pytest.mark.asyncio
async def test_classifier_load_crash_notice_says_it_will_keep_retrying(monkeypatch):
    from automation import update_failure_notice
    from jobs import corrections_recording

    delivered = AsyncMock()
    monkeypatch.setattr(corrections_recording, "owner_user_ids", lambda: ["owner"])
    monkeypatch.setattr(update_failure_notice, "_tell_conductor_about_owner_notice", delivered)

    await update_failure_notice.tell_conductor_about_corrections_classifier_load_crash("v2")

    assert delivered.await_args.args == (
        "owner", "Corrections classifier v2 keeps crashing on load and will keep retrying.",
    )
    assert delivered.await_args.kwargs == {
        "event_type": "system.corrections_classifier_load_crash",
        "delivery_id": "corrections-classifier-load-crash:v2",
        "notice_name": "corrections classifier load crash",
    }


@pytest.mark.asyncio
async def test_scheduled_lapse_skips_unavailable_workspace_conductor(isolated_db, monkeypatch):
    """A missing conductor logs the skip and cannot undo a cleared schedule."""
    from automation import update_failure_notice
    from core import agent_roster_queries
    from events import delivery

    warnings = []
    monkeypatch.setattr(
        agent_roster_queries,
        "corsia_agent_contact",
        lambda _user, _db: (None, "no workspace conductor"),
    )
    monkeypatch.setattr(delivery, "deliver_to_thread", AsyncMock())
    monkeypatch.setattr(
        update_failure_notice.logger,
        "warning",
        lambda message, *args: warnings.append(message.format(*args)),
    )

    await update_failure_notice.tell_conductor_about_scheduled_lapse(
        "missing",
        "Tonight's scheduled update did not run.",
        delivery_id="scheduled-update-lapsed:test",
    )

    assert warnings == ["scheduled update conductor notice skipped: no workspace conductor"]
    delivery.deliver_to_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_returns_while_the_conductor_notice_is_pending(isolated_db, monkeypatch):
    """A failed update's release answer never waits on a conductor turn, and the notice follows the resume."""
    from automation import restart, self_restart, update_failure_notice
    from infra import drain

    operation_id = self_restart.try_acquire(
        None, "steve", None, None, "update", kind="update", trigger="manual"
    )
    assert operation_id is not None
    order: list[str] = []
    notice_started = asyncio.Event()
    finish_notice = asyncio.Event()

    async def slow_notice(_operation_id: str) -> None:
        order.append("notice")
        notice_started.set()
        await finish_notice.wait()

    monkeypatch.setattr(update_failure_notice, "tell_conductor_about_failed_update", slow_notice)
    monkeypatch.setattr(drain, "cancel_drain_projection", lambda: order.append("clear"))
    monkeypatch.setattr(
        "automation.restart_settlement.resume_threads_detached", lambda threads, *_args: order.append("resume")
    )

    released = await asyncio.wait_for(
        restart.abort_restart_control(operation_id, error="build summary", failure_code="build_failed"),
        timeout=5,
    )
    await asyncio.wait_for(notice_started.wait(), timeout=5)

    assert released is True
    assert order == ["clear", "resume", "notice"]
    assert not finish_notice.is_set()
    finish_notice.set()
    await asyncio.gather(*tuple(restart._RESUME_TASKS))


@pytest.mark.asyncio
async def test_conductor_notice_failure_never_skips_resume(isolated_db, monkeypatch):
    """A notice that raises is logged on its own task and cannot undo the committed release or the resume."""
    from automation import restart, self_restart, update_failure_notice
    from infra import drain

    operation_id = self_restart.try_acquire(
        None, "steve", None, None, "update", kind="update", trigger="manual"
    )
    assert operation_id is not None
    monkeypatch.setattr(
        update_failure_notice, "tell_conductor_about_failed_update",
        AsyncMock(side_effect=RuntimeError("notice failed")),
    )
    cleared = []
    resumed = []
    monkeypatch.setattr(drain, "cancel_drain_projection", lambda: cleared.append(True))
    monkeypatch.setattr(
        "automation.restart_settlement.resume_threads_detached", lambda threads, *_args: resumed.append(tuple(threads))
    )

    released = await restart.abort_restart_control(
        operation_id, error="build summary", failure_code="build_failed"
    )
    await asyncio.gather(*tuple(restart._RESUME_TASKS), return_exceptions=True)

    assert released is True
    assert cleared == [True]
    assert len(resumed) == 1
