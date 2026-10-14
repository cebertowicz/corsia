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


# A staged update that a later restart activated is no longer a failure.


def _restart(*, settle: str | None) -> str:
    """One plain restart acquired after the current rows; `settle` is how it ended (None: still running)."""
    from automation import self_restart

    operation_id = self_restart.try_acquire(None, "steve", None, None, "restart", kind="restart")
    assert operation_id is not None
    if settle == "complete":
        for expected, target in [("holding", "steering"), ("steering", "resuming")]:
            assert self_restart.transition_operation(operation_id, expected=expected, target=target)
        assert self_restart.complete_operation(operation_id)
    elif settle == "failed":
        assert self_restart.abort_operation(operation_id, error="stopped", failure_code="launch_failed")
    return operation_id


def _failed_update(code: str) -> str:
    from automation import self_restart

    operation_id = _update()
    assert self_restart.abort_operation(operation_id, error="stopped", failure_code=code)
    return operation_id


def _projected_status() -> dict:
    from api.system_admin import system_update_status

    with patch("infra.update_status.update_status") as status:
        status.return_value.to_dict.return_value = {"available": False}
        return asyncio.run(system_update_status(user={}))["operation"]


def test_a_completed_restart_after_a_staged_update_failure_is_reported(isolated_db):
    failed = _failed_update("restart_pending")
    assert _projected_status()["activated_by_restart"] is False

    _restart(settle="complete")

    operation = _projected_status()
    assert operation["operation_id"] == failed
    assert operation["activated_by_restart"] is True


@pytest.mark.parametrize("settle", [None, "failed"])
def test_a_restart_that_did_not_finish_activates_nothing(isolated_db, settle):
    _failed_update("restart_pending")
    _restart(settle=settle)

    assert _projected_status()["activated_by_restart"] is False


@pytest.mark.parametrize("code", ["build_failed", "fetch_failed", "checkout_dirty", None])
def test_a_restart_does_not_clear_a_failure_it_could_not_have_fixed(isolated_db, code):
    _failed_update(code)
    _restart(settle="complete")

    assert _projected_status()["activated_by_restart"] is False


def test_a_restart_before_the_failure_does_not_clear_it(isolated_db):
    _restart(settle="complete")
    _failed_update("restart_pending")

    assert _projected_status()["activated_by_restart"] is False


def test_a_plain_restart_with_no_update_history_reports_no_operation(isolated_db):
    from api.system_admin import system_update_status

    _restart(settle="complete")

    with patch("infra.update_status.update_status") as status:
        status.return_value.to_dict.return_value = {"available": False}
        assert "operation" not in asyncio.run(system_update_status(user={}))
