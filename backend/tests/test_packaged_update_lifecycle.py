"""Packaged Update confirmation owns both durable outcomes and resumed input."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from automation import restart, restart_startup, self_restart
from auth_fixtures import OWNER_TOKEN
from infra.database import get_db
from infra.db_async import run_in_db


@pytest.fixture(autouse=True)
def isolated_projection(monkeypatch, tmp_path):
    from infra import drain, state

    monkeypatch.setattr(drain, "SIGNAL_FILE", tmp_path / "drain")
    monkeypatch.setattr(state, "broadcast_all", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(state, "broadcast_to_user", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("automation.update_failure_notice.tell_conductor_detached", lambda *_args: None)
    state._drain = None
    yield
    state._drain = None


def operation_in(phase="verifying", *, previous="0.4.1"):
    operation_id = self_restart.try_acquire(
        None, "steve", None, None, "update", kind="update",
        target_version="0.4.2", previous_version=previous,
    )
    with get_db() as db:
        db.execute("UPDATE self_restart_requests SET phase=? WHERE id=?", (phase, operation_id))
    return operation_id


# Independent contract: failure codes do not select the legal source phase.
MOVES = {
    "holding": {"steering", "resuming", "failed", "cancelled"},
    "steering": {"draining", "resuming", "failed", "cancelled"},
    "draining": {"ready", "resuming", "failed", "cancelled"},
    "ready": {"restarting", "resuming", "failed", "cancelled"},
    "restarting": {"resuming", "failed", "cancelled"},
    "resuming": {"verifying", "complete", "failed"},
    "verifying": {"complete", "failed"},
}


@pytest.mark.parametrize("source", MOVES)
@pytest.mark.parametrize("target", [*MOVES, "complete", "failed", "cancelled"])
def test_transition_contract(isolated_db, source, target):
    operation_id = operation_in(source)
    if target == "complete":
        moved = self_restart.complete_operation(operation_id)
    elif target in {"failed", "cancelled"}:
        moved = self_restart.abort_operation(
            operation_id, error="test failure", cancelled=target == "cancelled",
            failure_code="rolled_back",  # deliberately unrelated to transition legality
        )
    else:
        moved = self_restart.transition_operation(operation_id, expected=source, target=target)
    assert moved == (target in MOVES[source])


@pytest.mark.parametrize("terminal", ["complete", "failed", "cancelled", "superseded"])
def test_terminal_operations_cannot_move(isolated_db, terminal):
    operation_id = operation_in()
    with get_db() as db:
        db.execute("UPDATE self_restart_requests SET status=?,phase=NULL WHERE id=?", (terminal, operation_id))
    assert not self_restart.complete_operation(operation_id)
    assert not self_restart.abort_operation(operation_id, error="late failure")
    assert not self_restart.transition_operation(operation_id, expected="resuming", target="verifying")


async def test_shell_confirmation_resumes_actual_held_input(isolated_db, monkeypatch):
    from api.drain import router
    from infra.thread_driver import admit_input
    from infra.thread_inputs import start_idle_turn

    operation_id = await run_in_db(operation_in, "resuming")
    with get_db() as db:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("queued-update", "steve", "oMLX/qwen", "t0", "t0"),
        )
    admitted = await run_in_db(admit_input,
        "queued-update", role="user", content="held through Update", intent="apply_now",
        message_id="held-update-input", turn_id="held-turn", boot_epoch="boot", admitted_at="t1",
    )
    assert admitted.held_for_restart
    assert await run_in_db(self_restart.transition_operation, operation_id, expected="resuming", target="verifying")
    claimed = []

    async def drive(thread_id):
        claimed.append(await run_in_db(start_idle_turn, thread_id, turn_id="resumed-turn", boot_epoch="boot", started_at="t2"))

    monkeypatch.setattr("infra.thread_execution.drive_thread", drive)
    monkeypatch.setattr("infra.build_info.app_version", lambda: "0.4.2")
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/system/drain/update", params={"to" + "ken": OWNER_TOKEN},
            json={"operation_id": operation_id, "healthy": True},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "complete"
        if restart._RESUME_TASKS:
            await asyncio.gather(*tuple(restart._RESUME_TASKS))
        duplicate = await client.post(
            "/api/system/drain/update", params={"to" + "ken": OWNER_TOKEN},
            json={"operation_id": operation_id, "healthy": True},
        )
        assert duplicate.status_code == 200
    assert len(claimed) == 1
    assert claimed[0] is not None and claimed[0].turn_id == "resumed-turn"
    with get_db() as db:
        row = db.execute("SELECT active_turn_id FROM messages WHERE id='held-update-input'").fetchone()
        assert row[0] == "resumed-turn"
    assert not self_restart.restart_holds_new_work()


@pytest.mark.parametrize("running,previous,code", [
    (None, "0.4.1", "version_unverified"),
    ("broken", "0.4.1", "version_unverified"),
    ("0.4.0", "0.4.1", "wrong_version_running"),
    ("0.4.1", "0.4.1", "rolled_back"),
    ("0.4.1", None, "wrong_version_running"),
    ("0.4.2", "0.4.1", "version_unverified"),
])
def test_interrupted_confirmation_settles_without_holding(
    isolated_db, monkeypatch, tmp_path, running, previous, code,
):
    operation_id = operation_in(previous=previous)
    bundle = tmp_path / "CORSIA.app"
    bundle.mkdir()
    bundle.with_name("CORSIA.app.previous").mkdir()
    monkeypatch.setattr("infra.platform_paths.app_bundle_root", lambda: bundle)
    monkeypatch.setattr("infra.build_info.app_version", lambda: running)
    restart_startup.close_out_prior_self_restart()
    assert restart_startup._complete_restart_operation() == operation_id
    row = self_restart.operation_by_id(operation_id)
    assert row["status"] == "failed" and row["failure_code"] == code
    assert not self_restart.restart_holds_new_work()
    assert self_restart.previous_version(row) == previous


async def test_failure_report_releases_before_bundle_rollback(isolated_db, monkeypatch):
    from automation.update_verification import confirm_packaged_update

    operation_id = await run_in_db(operation_in)
    monkeypatch.setattr("infra.build_info.app_version", lambda: "0.4.2")
    monkeypatch.setattr("infra.thread_execution.drive_thread", AsyncMock())
    row = await confirm_packaged_update(operation_id, healthy=False, reason="health check failed")
    assert row["status"] == "failed" and row["failure_code"] == "version_unverified"
    assert json.loads(row["failure_detail"])["previous_version"] == "0.4.1"
    assert not self_restart.restart_holds_new_work()
    if restart._RESUME_TASKS:
        await asyncio.gather(*tuple(restart._RESUME_TASKS))


async def test_pre_protocol_update_mismatch_never_claims_rollback(isolated_db, monkeypatch):
    from automation.update_verification import confirm_packaged_update

    operation_id = await run_in_db(operation_in, previous=None)
    monkeypatch.setattr("infra.build_info.app_version", lambda: "0.4.1")
    row = await confirm_packaged_update(operation_id, healthy=False, reason="version mismatch")
    assert row["status"] == "failed"
    assert row["failure_code"] == "wrong_version_running"
    assert self_restart.previous_version(row) is None
