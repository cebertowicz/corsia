"""Refusals settle only the update that owns the lifecycle process."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from automation.update_log import record_update_log


@pytest.mark.asyncio
async def test_refusal_delete_settles_failed_with_reason(monkeypatch):
    from api import drain
    from automation import restart, self_restart

    monkeypatch.setattr(drain, "_require_user", lambda _token: {"id": "owner"})
    monkeypatch.setattr(self_restart, "operation_by_id", lambda _id: {"kind": "update"})
    abort = AsyncMock(return_value=True)
    monkeypatch.setattr(restart, "abort_restart_control", abort)
    result = await drain.cancel_drain("token", "a" * 32, failure_code="update_refused", reason="Prepared build is missing", backend_healthy=True)

    assert result["operation_id"] == "a" * 32
    assert abort.await_args.kwargs["failure_code"] == "update_refused"
    assert abort.await_args.kwargs["error"] == "Prepared build is missing"
    assert json.loads(abort.await_args.kwargs["failure_detail"])["backend_healthy"] is True
    assert not abort.await_args.kwargs.get("cancelled", False)


@pytest.mark.asyncio
async def test_regular_delete_still_cancels(monkeypatch):
    from api import drain
    from automation import restart

    monkeypatch.setattr(drain, "_require_user", lambda _token: {"id": "owner"})
    abort = AsyncMock(return_value=True)
    monkeypatch.setattr(restart, "abort_restart_control", abort)
    await drain.cancel_drain("token", "b" * 32)
    assert abort.await_args.kwargs == {"cancelled": True}


@pytest.mark.asyncio
async def test_unknown_refusal_code_rejected(monkeypatch):
    from api import drain

    monkeypatch.setattr(drain, "_require_user", lambda _token: {"id": "owner"})
    with pytest.raises(HTTPException):
        await drain.cancel_drain("token", "a" * 32, failure_code="arbitrary", reason="no")


@pytest.mark.parametrize("packaged", [False, True])
@pytest.mark.asyncio
async def test_unreported_refusal_on_restart_recovery(isolated_db, monkeypatch, tmp_path, packaged):
    """An unavailable DELETE leaves only the in-flight row for startup to adopt."""
    from automation import restart_startup, self_restart, update_verification
    from infra import build_info, config, platform_paths
    from infra.update_operation import project_update_operation

    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    operation_id = "d" * 32
    log = tmp_path / f"update-{operation_id}.log"
    log.write_text("Prepared update missing\n")
    operation_id = self_restart.try_acquire(
        None, "owner", None, None, "update", kind="update", target_version="1.2.3",
        previous_version="1.2.2", operation_id=operation_id,
    )
    assert operation_id
    assert record_update_log(operation_id, str(log))
    # The shell refused before stopping and could not reach DELETE; no DB mutation occurred.
    monkeypatch.setattr(platform_paths, "app_bundle_root", lambda: "/CORSIA.app" if packaged else None)
    monkeypatch.setattr(build_info, "app_version", lambda: "1.2.2")
    assert self_restart.adopt_for_resume() == operation_id
    restart_startup._complete_restart_operation()
    row = self_restart.operation_by_id(operation_id)
    if packaged:
        assert row["phase"] == "verifying"
        monkeypatch.setattr("infra.drain.cancel_drain_projection", lambda: None)
        monkeypatch.setattr("automation.restart_settlement.resume_threads_detached", lambda *_args: None)
        monkeypatch.setattr("automation.update_failure_notice.tell_conductor_detached", lambda *_args: None)
        await update_verification.confirm_packaged_update(operation_id, healthy=True, reason="confirmation_timeout")
        row = self_restart.operation_by_id(operation_id)
        assert row["status"] == "failed"
        assert row["failure_code"] == "rolled_back"
        assert "Prepared update missing" in project_update_operation(row)["log_tail"]
    else:
        assert row["status"] == "complete"
        assert project_update_operation(row)["log_tail"] is None


@pytest.mark.asyncio
async def test_acquired_update_owns_log_through_later_failure(isolated_db, monkeypatch, tmp_path):
    from api.settings_restart import _acquire_settings_restart
    from automation import self_restart
    from infra import config
    from infra.update_operation import project_update_operation

    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    first = await _acquire_settings_restart({"id": "owner"}, "update", [])
    first_row = self_restart.operation_by_id(first)
    assert first_row["failure_detail"] is None
    first_log = str(tmp_path / f"update-{first}.log")
    from pathlib import Path
    Path(first_log).write_text("first preparation\nfirst lifecycle\n")
    assert record_update_log(first, first_log)
    assert self_restart.abort_operation(first, error="first failure", failure_code="update_refused")
    assert "first lifecycle" in project_update_operation(self_restart.operation_by_id(first))["log_tail"]

    second = await _acquire_settings_restart({"id": "owner"}, "update", [])
    second_row = self_restart.operation_by_id(second)
    assert second_row["failure_detail"] is None
    second_log = str(tmp_path / f"update-{second}.log")
    assert first_log != second_log
    Path(second_log).write_text("second lifecycle\n")
    assert record_update_log(second, second_log)
    assert self_restart.abort_operation(second, error="second failure", failure_code="update_refused")
    assert project_update_operation(self_restart.operation_by_id(second))["log_tail"] == "second lifecycle\n"
    assert "second lifecycle" not in project_update_operation(self_restart.operation_by_id(first))["log_tail"]


@pytest.mark.asyncio
async def test_lifecycle_appends_to_preparation_log(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from api import settings_restart
    from infra import config

    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(settings_restart.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="preparation output", stderr=""))
    def spawn(_script, _args, _env, _id, log, **_kwargs):
        log.write("lifecycle output\n")
        return object()
    monkeypatch.setattr(settings_restart, "spawn_restart_lifecycle", spawn)
    operation_id = "c" * 32
    env = {"CORSIA_RESTART_OPERATION_ID": operation_id}
    settings_restart._prepare_current_release(Path("/tmp/corsia"), env)
    await settings_restart._launch_settings_restart(operation_id, True, Path("/tmp/corsia"), env, ["restart-prepared"], lambda name: tmp_path / name)
    log = tmp_path / f"update-{operation_id}.log"
    assert log.read_text() == "preparation output\nlifecycle output\n"
    assert not (tmp_path / "restart.log").exists()


@pytest.mark.parametrize("reachable", [True, False])
def test_lifecycle_refusal_logs_before_delete_even_when_backend_unreachable(tmp_path, monkeypatch, reachable):
    import os
    from api.restart_shell import spawn_restart_lifecycle

    curl = tmp_path / "curl"
    trace = tmp_path / "calls"
    curl.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$TRACE"\nexit ' + ('0' if reachable else '7') + '\n')
    curl.chmod(0o755)
    monkeypatch.setattr("api.restart_shell.shutil.which", lambda *_args, **_kwargs: str(curl))
    script = tmp_path / "corsia"
    script.write_text('#!/bin/bash\n_corsia_lifecycle_refuse "Prepared frontend build is missing"\n')
    script.chmod(0o755)
    log = tmp_path / ("update-" + "a" * 32 + ".log")
    with log.open("w") as out:
        process = spawn_restart_lifecycle(script, ["restart-prepared"], {
            **os.environ, "CORSIA_RESTART_BACKEND_PORT": "4111", "TRACE": str(trace)
        }, "a" * 32, out, caller_credential="test-credential")
        assert process.wait(timeout=5) != 0
    assert "Prepared frontend build is missing" in log.read_text()
    calls = trace.read_text()
    assert "operation_id=" + "a" * 32 in calls
    assert "failure_code=update_refused" in calls
    assert "reason=Prepared frontend build is missing" in calls
