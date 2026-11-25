"""Evidence for operation log ownership and early refusal boundaries."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.usefixtures("newer_build_available")


@pytest.mark.asyncio
async def test_disk_space_failure_does_not_link_a_log_that_was_never_created(isolated_db, tmp_path, monkeypatch):
    from api import settings_restart
    from automation import self_restart
    from infra import config
    from infra.database_migrations import BackupRoomError

    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr("infra.drain.cancel_drain_projection", lambda: None)
    monkeypatch.setattr("automation.restart_settlement.resume_threads_detached", lambda *_args: None)
    monkeypatch.setattr("automation.update_failure_notice.tell_conductor_detached", lambda *_args: None)
    monkeypatch.setattr(settings_restart, "_check_update_backup_room", AsyncMock(side_effect=BackupRoomError("full", 100, 25)))
    operation_id = await settings_restart._acquire_settings_restart({"id": "owner"}, "update", [])
    with pytest.raises(HTTPException):
        await settings_restart._check_room_or_compensate(operation_id, [operation_id])
    row = self_restart.operation_by_id(operation_id)
    detail = json.loads(row["failure_detail"])
    assert row["failure_code"] == "disk_space"
    assert detail == {"needed_bytes": 100, "free_bytes": 25}
    assert list(tmp_path.glob("update-*.log")) == []


@pytest.mark.asyncio
async def test_disk_space_after_staging_discards_bundle_before_drain(isolated_db, tmp_path, monkeypatch):
    from pathlib import Path
    from api import settings_restart
    from automation import self_restart
    from infra import config
    from infra.database_migrations import BackupRoomError

    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr("infra.drain.cancel_drain_projection", lambda: None)
    monkeypatch.setattr("automation.restart_settlement.resume_threads_detached", lambda *_args: None)
    monkeypatch.setattr("automation.update_failure_notice.tell_conductor_detached", lambda *_args: None)
    stage = tmp_path / "CORSIA.app.staged"
    script = tmp_path / "corsia"
    script.write_text(f'#!/bin/sh\n[ "$1" = discard-prepared ] || exit 1\nrm -rf "{stage}"\n')
    script.chmod(0o755)
    async def prepare(operation_id, *_args):
        stage.mkdir()
        settings_restart._update_log_path(operation_id).write_text("prepared")
    monkeypatch.setattr(settings_restart, "_prepare_owned_release", prepare)
    check = AsyncMock(side_effect=[None, BackupRoomError("full", 100, 25)])
    monkeypatch.setattr(settings_restart, "_check_update_backup_room", check)
    drain = AsyncMock()
    monkeypatch.setattr(settings_restart, "_begin_settings_drain", drain)

    env = {}
    with pytest.raises(HTTPException) as raised:
        await settings_restart.prelaunch_settings_restart(
            {"id": "owner"}, "update", script, env, ["restart"],
            lambda name: Path(name), caller_token="test",
        )
    assert raised.value.status_code == 409
    assert raised.value.detail == {"code": "disk_space", "needed_bytes": 100, "free_bytes": 25}
    row = self_restart.operation_by_id(env["CORSIA_RESTART_OPERATION_ID"])
    assert row["status"] == "failed"
    assert row["failure_code"] == "disk_space"
    detail = json.loads(row["failure_detail"])
    assert detail == {"needed_bytes": 100, "free_bytes": 25, "log_path": str(settings_restart._update_log_path(row["id"]))}
    assert not stage.exists()
    assert check.await_count == 2
    drain.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("stat failed"), asyncio.CancelledError()])
async def test_post_stage_check_error_discards_source_marker(isolated_db, tmp_path, monkeypatch, error):
    import shlex
    from pathlib import Path
    from api import settings_restart
    from automation import self_restart
    from infra import config

    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr("infra.drain.cancel_drain_projection", lambda: None)
    monkeypatch.setattr("automation.restart_settlement.resume_threads_detached", lambda *_args: None)
    monkeypatch.setattr("automation.update_failure_notice.tell_conductor_detached", lambda *_args: None)
    checkout = tmp_path / "checkout"
    stage = checkout / "frontend" / "node_modules" / ".corsia-dist-stage"
    marker = stage.with_name(stage.name + ".prepared")
    runtime = Path(__file__).resolve().parents[2] / "scripts" / "corsia.d" / "runtime.sh"
    script = tmp_path / "discard"
    script.write_text(f'#!/bin/bash\nsource {shlex.quote(str(runtime))}\ndiscard_redeploy_build {shlex.quote(str(checkout))}\n')
    script.chmod(0o755)

    async def prepare(*_args):
        stage.mkdir(parents=True)
        marker.touch()

    monkeypatch.setattr(settings_restart, "_prepare_owned_release", prepare)
    monkeypatch.setattr(settings_restart, "_check_update_backup_room", AsyncMock(side_effect=[None, error]))
    drain = AsyncMock()
    monkeypatch.setattr(settings_restart, "_begin_settings_drain", drain)
    env = {}
    with pytest.raises(type(error)):
        await settings_restart.prelaunch_settings_restart(
            {"id": "owner"}, "update", script, env, ["restart"],
            lambda name: tmp_path / name, caller_token="test",
        )
    row = self_restart.operation_by_id(env["CORSIA_RESTART_OPERATION_ID"])
    assert row["status"] == ("cancelled" if isinstance(error, asyncio.CancelledError) else "failed")
    assert not stage.exists()
    assert not marker.exists()
    drain.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifecycle_open_records_log_after_preparation_write_fails(isolated_db, tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from api import settings_restart
    from automation import self_restart
    from infra import config

    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(settings_restart.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="prepared", stderr=""))
    operation_id = await settings_restart._acquire_settings_restart({"id": "owner"}, "update", [])
    expected_log = settings_restart._update_log_path(operation_id)
    env = {"CORSIA_RESTART_OPERATION_ID": operation_id}
    with monkeypatch.context() as preparation:
        preparation.setattr(settings_restart, "nofollow_log_opener", lambda *_args: (_ for _ in ()).throw(OSError("disk write failed")))
        await settings_restart._prepare_owned_release(operation_id, [operation_id], Path("corsia"), env)
    assert not expected_log.exists()
    assert not json.loads(self_restart.operation_by_id(operation_id)["failure_detail"] or "{}").get("log_path")

    def spawn(_script, _args, _env, op_id, log_file, **_kwargs):
        detail = json.loads(self_restart.operation_by_id(op_id)["failure_detail"])
        assert detail["log_path"] == str(expected_log)
        log_file.write("refused\n")
        return object()

    monkeypatch.setattr(settings_restart, "spawn_restart_lifecycle", spawn)
    await settings_restart._launch_settings_restart(operation_id, True, Path("corsia"), env, [], lambda _: tmp_path / "restart.log")
    assert expected_log.read_text() == "refused\n"
    assert list(tmp_path.glob("*.log")) == [expected_log]


@pytest.mark.parametrize("reachable", [True, False])
def test_explicit_refusal_records_result_without_cleanup_request(tmp_path, monkeypatch, reachable):
    import os
    from api.restart_shell import spawn_restart_lifecycle

    calls = tmp_path / "calls"
    curl = tmp_path / "curl"
    curl.write_text('#!/bin/bash\nprintf "call\\n" >> "$CALLS"\nexit ' + ('0' if reachable else '7') + '\n')
    curl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    script = tmp_path / "corsia"
    from pathlib import Path
    corsia_text = (Path(__file__).resolve().parents[2] / "scripts/corsia").read_text()
    fail = "fail() {" + corsia_text.split("fail() {", 1)[1].split("\n}", 1)[0] + "\n}"
    script.write_text(f'#!/bin/bash\nRED=""; NC=""\n{fail}\nfail "The staged update is missing"\nexit 1\n')
    script.chmod(0o755)
    log = tmp_path / "update.log"
    with log.open("w") as out:
        child = spawn_restart_lifecycle(script, ["restart-prepared"], {
            **os.environ, "CORSIA_RESTART_BACKEND_PORT": "4111", "CALLS": str(calls),
        }, "a" * 32, out, caller_credential="test-credential")
        assert child.wait(timeout=5) != 0
    assert not calls.exists()
    assert "The staged update is missing" in log.read_text()
    assert "refusal_reason=The staged update is missing" in child.lifecycle_result_path.read_text()
