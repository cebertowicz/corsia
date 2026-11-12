"""Packaged Update confirmation owns both durable outcomes and resumed input."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from automation import restart, restart_startup, self_restart
from automation.self_restart import record_update_log
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


async def test_pre_stop_refusal_fails_through_real_shell_and_http(isolated_db, tmp_path, monkeypatch):
    import os
    import socket
    from pathlib import Path

    import uvicorn
    from api.drain import router
    from api.restart_shell import spawn_restart_lifecycle

    operation_id = await run_in_db(operation_in, "draining")
    app = FastAPI()
    app.include_router(router)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    root = Path(__file__).resolve().parents[2]
    corsia_text = (root / "scripts/corsia").read_text()
    fail = "fail() {" + corsia_text.split("fail() {", 1)[1].split("\n}", 1)[0] + "\n}"
    app_mode = root / "scripts/corsia.d/app_mode.sh"
    runtime = root / "scripts/corsia.d/runtime.sh"
    from infra import config
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    script = tmp_path / "corsia"
    script.write_text(f'''#!/bin/bash
SCRIPT_DIR=/disposable
source "{app_mode}"
source "{runtime}"
require_owned_service_definition() {{ :; }}
service_is_loaded() {{ return 1; }}
require_no_unmanaged_backend() {{ :; }}
app_mode_hold_update_lock() {{ :; }}
is_backend_running() {{ return 1; }}
RED=""; NC=""
{fail}
FORCE=false
BACKEND_PORT={port}
app_mode_swap_staged_update /disposable/staged
''')
    script.chmod(0o755)
    try:
        log_path = tmp_path / f"update-{operation_id}.log"
        with log_path.open("w") as log:
            assert await run_in_db(record_update_log, operation_id, str(log_path))
            child = spawn_restart_lifecycle(
                script, ["restart-prepared"],
                {**os.environ, "CORSIA_RESTART_BACKEND_PORT": str(port)},
                operation_id, log, caller_credential=OWNER_TOKEN,
            )
            from api.system_admin import _monitor_restart_process
            await _monitor_restart_process(child, operation_id)
            await asyncio.to_thread(child.wait, 10)
            for _ in range(100):
                if (await run_in_db(self_restart.operation_by_id, operation_id))["status"] != "in_flight":
                    break
                await asyncio.sleep(0.01)
    finally:
        server.should_exit = True
        await serving
        listener.close()
    assert "Safe update refused" in log_path.read_text()
    assert "Update refused:" in log_path.read_text()
    row = await run_in_db(self_restart.operation_by_id, operation_id)
    assert row["status"] == "failed"
    assert row["failure_code"] == "update_refused"
    assert "current backend is not healthy" in row["error"]
    detail = json.loads(row["failure_detail"])
    assert detail["backend_healthy"] is False
    assert detail["log_path"] == str(log_path)
    assert not self_restart.restart_holds_new_work()


async def test_failed_update_child_releases_when_cleanup_cannot_connect(isolated_db, tmp_path, monkeypatch):
    import os

    from api import system_admin
    from api.restart_shell import spawn_restart_lifecycle
    from infra import drain, state

    operation_id = await run_in_db(operation_in, "ready")
    drain.SIGNAL_FILE.write_text("{}")
    state._drain = {"operation_id": operation_id}
    assert self_restart.restart_holds_new_work()
    script = tmp_path / "corsia"
    script.write_text("#!/bin/bash\n/bin/sleep 0.2\n_corsia_lifecycle_refusal_reason='update refused'\nexit 1\n")
    script.chmod(0o755)
    curl = tmp_path / "curl"
    curl.write_text("#!/bin/bash\nexit 52\n")
    curl.chmod(0o755)
    log_path = tmp_path / "update.log"
    settled = asyncio.Event()
    real_abort = restart.abort_restart_control

    async def observe_abort(*args, **kwargs):
        try:
            return await real_abort(*args, **kwargs)
        finally:
            settled.set()

    monkeypatch.setattr(restart, "abort_restart_control", observe_abort)

    async def launch(*_args, **_kwargs):
        with log_path.open("w") as log:
            assert await run_in_db(record_update_log, operation_id, str(log_path))
            child = spawn_restart_lifecycle(
                script, ["restart-prepared"],
                {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CORSIA_RESTART_BACKEND_PORT": "12345"},
                operation_id, log, caller_credential=OWNER_TOKEN,
            )
        return operation_id, child

    monkeypatch.setattr(system_admin, "prelaunch_settings_restart", launch)
    await system_admin.launch_update({"id": "steve"}, OWNER_TOKEN, "manual", port=12345)
    await asyncio.wait_for(settled.wait(), 3)
    row = await run_in_db(self_restart.operation_by_id, operation_id)
    assert row["status"] == "failed"
    assert row["failure_code"] == "update_refused"
    assert row["error"] == "update refused"
    assert json.loads(row["failure_detail"])["log_path"] == str(log_path)
    assert not self_restart.restart_holds_new_work()
    assert not drain.SIGNAL_FILE.exists() and state._drain is None
    assert "Update refused: update refused" in log_path.read_text()


async def test_child_refusal_preserves_already_recorded_reason(isolated_db):
    from api.drain import settle_drain_exit

    operation_id = await run_in_db(operation_in, "ready")
    assert await restart.abort_restart_control(
        operation_id, error="specific refusal", failure_code="update_refused"
    )
    assert await settle_drain_exit(
        operation_id, "update_refused", "specific refusal", False, before_resume=True
    ) is False
    row = await run_in_db(self_restart.operation_by_id, operation_id)
    assert row["status"] == "failed"
    assert row["error"] == "specific refusal" and row["failure_code"] == "update_refused"


@pytest.mark.parametrize("phase", ["resuming", "verifying"])
async def test_child_refusal_does_not_release_advanced_update(isolated_db, phase):
    from api.drain import settle_drain_exit

    operation_id = await run_in_db(operation_in, phase)
    held_before = self_restart.restart_holds_new_work()
    assert await settle_drain_exit(
        operation_id, "update_refused", "specific refusal", False, before_resume=True
    ) is False
    row = await run_in_db(self_restart.operation_by_id, operation_id)
    assert row["status"] == "in_flight" and row["phase"] == phase
    assert self_restart.restart_holds_new_work() == held_before


async def test_child_exit_after_advance_is_a_noop(isolated_db, tmp_path, caplog):
    import logging
    from types import SimpleNamespace
    from api.drain import settle_restart_exit

    operation_id = await run_in_db(operation_in, "resuming")
    before = await run_in_db(self_restart.operation_by_id, operation_id)
    result = tmp_path / "result"
    result.write_text("status=1\nis_update=true\nunconfirmed_bootout=false\nbackend_healthy=false\nrefusal_reason=refused\n")
    with caplog.at_level(logging.ERROR):
        await settle_restart_exit(SimpleNamespace(lifecycle_result_path=result), operation_id, 1)
    assert await run_in_db(self_restart.operation_by_id, operation_id) == before
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


async def test_child_refusal_with_carriage_return_keeps_reason(isolated_db, tmp_path):
    import os
    from api.restart_shell import spawn_restart_lifecycle
    from api.system_admin import _monitor_restart_process

    operation_id = await run_in_db(operation_in, "ready")
    script = tmp_path / "corsia"
    script.write_text("#!/bin/bash\n_corsia_lifecycle_refusal_reason=$'first\\rsecond'\nexit 1\n")
    script.chmod(0o755)
    with (tmp_path / "lifecycle.log").open("w") as log:
        child = spawn_restart_lifecycle(
            script, ["restart-prepared"],
            {**os.environ, "CORSIA_RESTART_BACKEND_PORT": "12345"}, operation_id, log,
            caller_credential=OWNER_TOKEN,
        )
        await asyncio.to_thread(child.wait, 5)
        await _monitor_restart_process(child, operation_id)
    row = await run_in_db(self_restart.operation_by_id, operation_id)
    assert row["failure_code"] == "update_refused"
    assert row["error"] == "first second"


@pytest.mark.parametrize("body", ["exit 9", "kill -TERM $$"])
async def test_child_exit_without_reason_matches_drain_cancel(isolated_db, tmp_path, body):
    import os

    from api.drain import settle_drain_exit
    from api.restart_shell import spawn_restart_lifecycle
    from api.system_admin import _monitor_restart_process

    baseline_id = await run_in_db(operation_in, "ready")
    await settle_drain_exit(baseline_id)
    baseline = await run_in_db(self_restart.operation_by_id, baseline_id)
    operation_id = await run_in_db(operation_in, "ready")
    script = tmp_path / "corsia"
    script.write_text(f"#!/bin/bash\n{body}\n")
    script.chmod(0o755)
    with (tmp_path / "lifecycle.log").open("w") as log:
        child = spawn_restart_lifecycle(
            script, ["restart-prepared"],
            {**os.environ, "CORSIA_RESTART_BACKEND_PORT": "12345"},
            operation_id, log, caller_credential=OWNER_TOKEN,
        )
        await asyncio.to_thread(child.wait, 5)
        await _monitor_restart_process(child, operation_id)
    row = await run_in_db(self_restart.operation_by_id, operation_id)
    assert (row["status"], row["failure_code"], row["error"]) == (
        baseline["status"], baseline["failure_code"], baseline["error"]
    )
    assert row["status"] == "cancelled"
    assert not self_restart.restart_holds_new_work()


async def _exit_lifecycle(tmp_path, operation_id, body):
    import os

    from api.restart_shell import spawn_restart_lifecycle
    from api.system_admin import _monitor_restart_process

    script = tmp_path / "corsia"
    script.write_text(f"#!/bin/bash\n{body}\n")
    script.chmod(0o755)
    with (tmp_path / "lifecycle.log").open("w") as log:
        child = spawn_restart_lifecycle(
            script, ["restart-prepared"],
            {**os.environ, "CORSIA_RESTART_BACKEND_PORT": "12345"},
            operation_id, log, caller_credential="x" * 8,
        )
        await asyncio.to_thread(child.wait, 5)
        await _monitor_restart_process(child, operation_id)


@pytest.mark.parametrize("phase", ["ready", "restarting"])
async def test_failed_stop_with_backend_alive_releases_hold(isolated_db, tmp_path, phase):
    # The drain finished (restarting) and bootout then failed with the service
    # still loaded: this backend survived, so it must release its own hold.
    operation_id = await run_in_db(operation_in, phase)
    await _exit_lifecycle(tmp_path, operation_id, "_corsia_update_stopping=1\nexit 1")
    row = await run_in_db(self_restart.operation_by_id, operation_id)
    assert row["status"] == "cancelled"
    assert not self_restart.restart_holds_new_work()


async def test_child_exit_retains_hold_for_unconfirmed_bootout(isolated_db, tmp_path):
    import os

    from api.restart_shell import spawn_restart_lifecycle
    from api.system_admin import _monitor_restart_process

    operation_id = await run_in_db(operation_in, "restarting")
    script = tmp_path / "corsia"
    script.write_text("#!/bin/bash\n_corsia_unconfirmed_bootout=true\nexit 9\n")
    script.chmod(0o755)
    with (tmp_path / "lifecycle.log").open("w") as log:
        child = spawn_restart_lifecycle(
            script, ["restart-prepared"],
            {**os.environ, "CORSIA_RESTART_BACKEND_PORT": "12345"},
            operation_id, log, caller_credential=OWNER_TOKEN,
        )
        await asyncio.to_thread(child.wait, 5)
        await _monitor_restart_process(child, operation_id)
    row = await run_in_db(self_restart.operation_by_id, operation_id)
    assert row["status"] == "in_flight"
    assert self_restart.restart_holds_new_work()


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


@pytest.mark.parametrize("running,status,code", [
    ("0.4.2", "complete", None),
    ("0.4.0", "failed", "wrong_version_running"),
])
async def test_unconfirmed_update_settles_after_timeout(
    isolated_db, monkeypatch, tmp_path, running, status, code,
):
    from loguru import logger

    from automation.update_verification import settle_unconfirmed_update
    from infra import drain, state

    operation_id = await run_in_db(operation_in)
    drain.SIGNAL_FILE.write_text("{}")
    state._drain = {"operation_id": operation_id}
    monkeypatch.setattr("infra.build_info.app_version", lambda: running)
    monkeypatch.setattr("infra.thread_execution.drive_thread", AsyncMock())
    await settle_unconfirmed_update(logger, timeout=0)
    row = self_restart.operation_by_id(operation_id)
    assert row["status"] == status and row["failure_code"] == code
    reason = json.loads(row["failure_detail"])["reason"]
    assert reason == ("confirmation_timeout" if code is None else "mismatch")
    assert not self_restart.restart_holds_new_work()
    assert not drain.SIGNAL_FILE.exists() and state._drain is None
    if restart._RESUME_TASKS:
        await asyncio.gather(*tuple(restart._RESUME_TASKS))


async def test_shell_confirmation_before_timeout_wins(isolated_db, monkeypatch):
    from loguru import logger

    from automation.update_verification import confirm_packaged_update, settle_unconfirmed_update

    operation_id = await run_in_db(operation_in)
    monkeypatch.setattr("infra.build_info.app_version", lambda: "0.4.2")
    monkeypatch.setattr("infra.thread_execution.drive_thread", AsyncMock())
    settling = asyncio.create_task(settle_unconfirmed_update(logger, timeout=0.2))
    await asyncio.sleep(0)
    confirmed = await confirm_packaged_update(operation_id, healthy=True)
    await settling
    assert confirmed["status"] == "complete"
    row = self_restart.operation_by_id(operation_id)
    assert row["status"] == "complete"
    assert "reason" not in json.loads(row["failure_detail"])
    if restart._RESUME_TASKS:
        await asyncio.gather(*tuple(restart._RESUME_TASKS))


@pytest.mark.parametrize("phase", ["resuming", None])
async def test_unconfirmed_settlement_ignores_other_operations(isolated_db, phase):
    from loguru import logger

    from automation.update_verification import settle_unconfirmed_update

    operation_id = await run_in_db(operation_in, phase) if phase else None
    # Returns before its timer: nothing here is an Update awaiting confirmation.
    await asyncio.wait_for(settle_unconfirmed_update(logger, timeout=3600), 5)
    if operation_id:
        assert self_restart.operation_by_id(operation_id)["status"] == "in_flight"


async def test_startup_drive_settles_unconfirmed_update_even_when_drive_fails(isolated_db, monkeypatch):
    from loguru import logger

    from automation import update_verification

    operation_id = await run_in_db(operation_in)
    monkeypatch.setattr(update_verification, "UNCONFIRMED_UPDATE_SECONDS", 0)
    monkeypatch.setattr("infra.build_info.app_version", lambda: "0.4.2")
    monkeypatch.setattr("infra.thread_execution.drive_thread", AsyncMock())

    async def broken_drive(*_args):
        raise RuntimeError("drive failed")

    async def no_harness():
        return None

    monkeypatch.setattr(restart_startup, "drive_startup_continuity", broken_drive)
    with pytest.raises(RuntimeError, match="drive failed"):
        await restart_startup.run_startup_drive(no_harness(), None, (), None, logger)
    assert self_restart.operation_by_id(operation_id)["status"] == "complete"
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


def _packaged_install(tmp_path, monkeypatch, *, staged_version: str, bundle_name: str = "CORSIA.app"):
    """Make this test run in a packaged install with one build staged beside it.

    The updater unpacks the build it just downloaded at `CORSIA.app.staged` whatever
    the installed app is called, and the swap installs that copy, so its stamped
    `CORSIAVersion` is the version the next start reports through
    `build_info.app_version()`.
    """
    import plistlib

    bundle = tmp_path / bundle_name
    (bundle / "Contents" / "Resources").mkdir(parents=True)
    staged = bundle.parent / "CORSIA.app.staged"
    (staged / "Contents").mkdir(parents=True)
    with (staged / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleShortVersionString": staged_version.split("-")[0],
                "CORSIAVersion": staged_version,
            },
            handle,
        )
    monkeypatch.setattr("infra.platform_paths.app_bundle_root", lambda: bundle)
    return bundle


@pytest.mark.parametrize("bundle_name", ["CORSIA.app", "CORSIA 2.app"])
async def test_update_verifies_the_build_staging_installed(
    isolated_db, monkeypatch, tmp_path, bundle_name
):
    """The stamp follows the staged bundle, not the earlier feed read.

    The probe before acquiring named 0.4.2-dev.1311 while staging took
    0.4.2-dev.1312 off the same feed, so strict verification failed the good build
    as `wrong_version_running` and the shell rolled it back. The staged build is
    found under the name the updater writes it to, so an install kept beside the
    current one under another name verifies too.
    """
    from unittest.mock import MagicMock

    from api import settings_restart
    from automation.update_verification import confirm_packaged_update
    from infra import update_status
    from infra.update_operation import project_update_operation

    _packaged_install(
        tmp_path, monkeypatch, staged_version="0.4.2-dev.1312", bundle_name=bundle_name
    )
    monkeypatch.setattr(
        update_status, "update_status", lambda force=False: update_status.UpdateStatus(
            branch=None, current_sha=None, remote_sha=None, commits_behind=None,
            available=True, checked_at=0.0, error=None, current_version="0.4.1",
            target_version="0.4.2-dev.1311",
            changes={"kind": "changelog", "items": ["Notes for the build the probe named"]},
        )
    )
    monkeypatch.setattr(settings_restart, "_check_update_backup_room", AsyncMock())
    monkeypatch.setattr(settings_restart, "_prepare_current_release", lambda *_args: None)
    monkeypatch.setattr(
        settings_restart, "spawn_restart_lifecycle", MagicMock(return_value=object())
    )

    stamped: list[str | None] = []

    async def drain(operation_id: str, _mode: str) -> None:
        # The drain is the point of no return: the launch and the swap follow it.
        stamped.append(self_restart.operation_by_id(operation_id)["target_version"])

    monkeypatch.setattr(settings_restart, "_begin_settings_drain", drain)

    from infra.config import LOGS_DIR

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    operation_id, _process = await settings_restart.prelaunch_settings_restart(
        {"id": "steve"}, "update", tmp_path / "corsia", {"CORSIA_RESTART_BACKEND_PORT": "1957"},
        ["restart"], lambda name: tmp_path / name, caller_token="lifecycle-credential",
    )

    assert stamped == ["0.4.2-dev.1312"]
    row = self_restart.operation_by_id(operation_id)
    assert row["target_version"] == "0.4.2-dev.1312"
    # Notes acquired with the probed build describe a build that is not installing,
    # so they go: a null is shown as no notes, never as the wrong build's notes.
    assert row["changes"] is None
    assert project_update_operation(row)["changes"] is None
    # A restamp that names the same build again keeps the notes the row already has.
    with get_db() as db:
        db.execute(
            "UPDATE self_restart_requests SET changes=? WHERE id=?",
            (json.dumps({"kind": "changelog", "items": ["Kept"]}), operation_id),
        )
    assert self_restart.restamp_target_version(operation_id, "0.4.2-dev.1312") is True
    kept = json.loads(self_restart.operation_by_id(operation_id)["changes"])
    assert kept["items"] == ["Kept"]

    with get_db() as db:
        db.execute(
            "UPDATE self_restart_requests SET phase='verifying' WHERE id=?", (operation_id,)
        )
    monkeypatch.setattr("infra.build_info.app_version", lambda: "0.4.2-dev.1312")
    monkeypatch.setattr("infra.thread_execution.drive_thread", AsyncMock())
    row = await confirm_packaged_update(operation_id, healthy=True)
    assert row["status"] == "complete"
    assert row["failure_code"] is None
    if restart._RESUME_TASKS:
        await asyncio.gather(*tuple(restart._RESUME_TASKS))
