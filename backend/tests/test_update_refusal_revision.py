"""Evidence for operation log ownership and early refusal boundaries."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


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
async def test_lifecycle_open_records_log_after_preparation_write_fails(isolated_db, tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from api import settings_restart
    from automation import self_restart
    from infra import config

    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(settings_restart.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="prepared", stderr=""))
    monkeypatch.setattr(Path, "write_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk write failed")))
    operation_id = await settings_restart._acquire_settings_restart({"id": "owner"}, "update", [])
    expected_log = settings_restart._update_log_path(operation_id)
    env = {"CORSIA_RESTART_OPERATION_ID": operation_id}
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
def test_explicit_refusal_attempts_exactly_one_cleanup(tmp_path, monkeypatch, reachable):
    import os
    from api.restart_shell import spawn_restart_lifecycle

    calls = tmp_path / "calls"
    curl = tmp_path / "curl"
    curl.write_text('#!/bin/bash\nprintf "call\\n" >> "$CALLS"\nexit ' + ('0' if reachable else '7') + '\n')
    curl.chmod(0o755)
    monkeypatch.setattr("api.restart_shell.shutil.which", lambda *_args, **_kwargs: str(curl))
    script = tmp_path / "corsia"
    script.write_text('#!/bin/bash\n_corsia_lifecycle_refuse "The staged update is missing"\n')
    script.chmod(0o755)
    log = tmp_path / "update.log"
    with log.open("w") as out:
        child = spawn_restart_lifecycle(script, ["restart-prepared"], {
            **os.environ, "CORSIA_RESTART_BACKEND_PORT": "4111", "CALLS": str(calls),
        }, "a" * 32, out, caller_credential="test-credential")
        assert child.wait(timeout=5) != 0
    assert calls.read_text().splitlines() == ["call"]
    assert "The staged update is missing" in log.read_text()
    assert ("cleanup curl exit 7" in log.read_text()) is (not reachable)
