"""S1 durable Update failure causes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_update_disk_refusal_persists_numbers_before_any_drain(isolated_db, monkeypatch):
    """A prepared update with no backup room releases its hold before steering."""
    from api import settings_restart
    from infra.database import get_db
    from infra.database_migrations import BackupRoomError

    monkeypatch.setattr(settings_restart, "_prepare_owned_release", AsyncMock())
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
    drain.assert_not_awaited()


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
        agent_roster_queries, "workspace_conductor_contact", lambda _user, _db: ({"id": "conductor"}, None)
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
        agent_roster_queries, "workspace_conductor_contact", lambda _user, _db: ({"id": "conductor"}, None)
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
