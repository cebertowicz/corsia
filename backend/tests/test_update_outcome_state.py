"""The Update operation row owns the outcome state every client reads."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

CHANGES = {"kind": "changelog", "headline": "Useful release", "items": ["First", "Second"], "more": 0}


def _row(operation_id: str) -> dict:
    from infra.database import get_db

    connection = get_db()
    try:
        return dict(connection.execute(
            "SELECT * FROM self_restart_requests WHERE id=?", (operation_id,)
        ).fetchone())
    finally:
        connection.close()


def _update(**fields) -> str:
    from automation import self_restart

    operation_id = self_restart.try_acquire(
        None, "steve", None, None, "update", kind="update", trigger="manual", **fields
    )
    assert operation_id is not None
    return operation_id


def test_operation_stores_the_offered_changes_and_projects_them(isolated_db):
    from infra.update_operation import project_update_operation

    operation_id = _update(target_version="1.2.0", changes=json.dumps(CHANGES))

    projected = project_update_operation(_row(operation_id))

    assert projected["changes"] == CHANGES
    assert projected["outcome_seen_at"] is None


@pytest.mark.parametrize("stored", [None, "not json", json.dumps(["a list"])])
def test_projection_has_no_changes_when_none_were_stored(isolated_db, stored):
    from infra.update_operation import project_update_operation

    operation_id = _update(changes=stored)

    assert project_update_operation(_row(operation_id))["changes"] is None


def test_outcome_seen_is_refused_for_unknown_and_unsettled_operations(isolated_db):
    from automation.update_outcome import mark_outcome_seen

    operation_id = _update()

    assert mark_outcome_seen("0" * 32) is None
    assert mark_outcome_seen(operation_id) is None
    assert _row(operation_id)["outcome_seen_at"] is None


@pytest.mark.parametrize("settle", ["complete", "failed"])
def test_first_client_to_mark_the_outcome_wins(isolated_db, settle):
    from automation import self_restart
    from automation.update_outcome import mark_outcome_seen
    from infra.database import write_transaction

    operation_id = _update()
    with write_transaction(None, scope="test") as connection:
        connection.execute(
            "UPDATE self_restart_requests SET status=?,phase=NULL,settled_at='then' WHERE id=?",
            (settle, operation_id),
        )

    assert mark_outcome_seen(operation_id) is True
    first = _row(operation_id)["outcome_seen_at"]
    assert first is not None
    assert mark_outcome_seen(operation_id) is True
    assert _row(operation_id)["outcome_seen_at"] == first
    assert self_restart.latest_update_operation()["outcome_seen_at"] == first


@pytest.mark.asyncio
async def test_outcome_seen_route_answers_404_for_an_unsettled_operation(isolated_db):
    from api.system_admin import system_update_outcome_seen

    operation_id = _update()

    with pytest.raises(HTTPException) as raised:
        await system_update_outcome_seen(operation_id, user={})

    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_outcome_seen_route_marks_a_settled_operation(isolated_db):
    from api.system_admin import system_update_outcome_seen
    from automation import self_restart

    operation_id = _update()
    self_restart.abort_operation(operation_id, error="stopped", failure_code="build_failed")

    assert await system_update_outcome_seen(operation_id, user={}) == {
        "operation_id": operation_id, "seen": True,
    }
    assert _row(operation_id)["outcome_seen_at"] is not None


def test_failure_log_tail_reads_only_the_end_of_a_large_log(tmp_path, monkeypatch):
    from infra import config
    from infra.update_operation import project_update_operation

    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    log = tmp_path / "update.log"
    log.write_bytes(b"old\n" * 2_000_000 + b"the last line\n")
    operation = {
        "id": "operation", "status": "failed", "failure_detail": json.dumps({"log_path": str(log)}),
    }

    with patch.object(Path, "read_text", side_effect=AssertionError("the whole log was read")):
        tail = project_update_operation(operation)["log_tail"]

    assert len(tail) == 6000
    assert tail.endswith("the last line\n")


# Every terminal path persists its cause.


@pytest.fixture
def quiet_release(monkeypatch):
    from automation import restart, update_failure_notice
    from infra import drain

    monkeypatch.setattr(drain, "cancel_drain_projection", lambda: None)
    monkeypatch.setattr(restart, "resume_threads_detached", lambda *_args: None)
    monkeypatch.setattr(
        update_failure_notice, "tell_conductor_about_failed_update", AsyncMock(return_value=None)
    )


@pytest.mark.asyncio
async def test_drain_setup_failure_keeps_its_exception_class(isolated_db, quiet_release, monkeypatch):
    from automation import restart

    operation_id = _update()
    def refuse(_operation_id):
        raise KeyError("steer")

    monkeypatch.setattr(restart, "_enter_steering", refuse)
    with pytest.raises(KeyError):
        await restart.begin_restart_drain("update", operation_id=operation_id)

    row = _row(operation_id)
    assert row["status"] == "failed"
    assert row["failure_code"] == "drain_failed"
    assert json.loads(row["failure_detail"]) == {"exception": "KeyError"}


@pytest.mark.asyncio
async def test_an_immediately_exiting_restart_script_names_launch_failed(isolated_db, quiet_release):
    from api.system_admin import _monitor_restart_process

    operation_id = _update()

    class Exited:
        def poll(self):
            return 3

    await _monitor_restart_process(Exited(), operation_id)

    row = _row(operation_id)
    assert row["failure_code"] == "launch_failed"
    assert json.loads(row["failure_detail"]) == {"exit_code": 3}


@pytest.mark.asyncio
async def test_a_client_disconnect_during_acquire_is_a_named_cancellation(isolated_db, quiet_release):
    from api.settings_restart import _compensate_cancelled_acquire

    operation_id = _update()

    await _compensate_cancelled_acquire([operation_id], asyncio.CancelledError())

    row = _row(operation_id)
    assert row["status"] == "cancelled"
    assert row["failure_code"] == "drain_cancelled"


def _prepare_failure(**run) -> HTTPException:
    import api.settings_restart as settings_restart

    with (
        patch("subprocess.run", **run),
        patch("api.settings_restart._update_log_path", return_value=Path("/nonexistent-dir/x/update.log")),
        pytest.raises(HTTPException) as raised,
    ):
        settings_restart._prepare_current_release(Path("/tmp/corsia"), {})
    return raised.value


def test_a_script_that_cannot_start_names_prepare_failed():
    assert _prepare_failure(side_effect=OSError("no such file")).update_failure_code == "prepare_failed"


def test_an_unwritable_log_keeps_the_refusal_the_script_reported(monkeypatch):
    completed = subprocess.CompletedProcess(args=[], returncode=45, stdout="", stderr="dirty")
    with patch("pathlib.Path.mkdir", side_effect=OSError("read-only")):
        failure = _prepare_failure(return_value=completed)

    assert failure.status_code == 500
    assert failure.update_failure_code == "checkout_dirty"
