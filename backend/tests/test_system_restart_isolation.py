from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock, mock_open, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth_fixtures import OWNER_SESSION_TOKEN, OWNER_TOKEN
from infra.default_port import DEFAULT_BACKEND_PORT


def _valid_token() -> str:
    return OWNER_SESSION_TOKEN


def _client(port: int) -> TestClient:
    from main import app

    return TestClient(app, base_url=f"http://testserver:{port}")


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    from infra.database import get_db

    db = get_db()
    try:
        return [dict(row) for row in db.execute(sql, params).fetchall()]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_lifecycle_shell_owns_pre_wrapper_failure_and_scrubs_credential(
    tmp_path: Path,
    monkeypatch,
):
    """A real shell failure before CLI dispatch releases only its exact operation."""
    from api.settings_restart import _launch_settings_restart

    operation_id = "d" * 32
    secret = "caller credential with spaces & symbols"
    child_env = tmp_path / "child.env"
    curl_result = tmp_path / "curl-result"
    expected_secret = tmp_path / "expected-secret"
    expected_secret.write_text(secret)

    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/bin/bash\n"
        "expected=$(cat \"$EXPECTED_SECRET_FILE\")\n"
        "args=$(printf '%s\\n' \"$@\")\n"
        "case \"$args\" in *\"operation_id=$EXPECTED_OPERATION\"*) op=ok ;; *) op=bad ;; esac\n"
        "case \"$args\" in *\"token=$expected\"*) auth=ok ;; *) auth=bad ;; esac\n"
        "printf '%s:%s:%s:%s:%s:%s\\n' \"$op\" \"$auth\" "
        "\"${CORSIA_RESTART_CALLER_TOKEN-unset}\" "
        "\"${CORSIA_TOKEN-unset}\" "
        "\"${_corsia_lifecycle_token-unset}\" "
        "\"${_corsia_cleanup_token-unset}\" > \"$CURL_RESULT\"\n"
    )
    fake_curl.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: str(fake_curl))

    failing_script = tmp_path / "corsia"
    failing_script.write_text(
        "#!/bin/bash\n"
        "env > \"$CHILD_ENV\"\n"
        "echo 'pre-wrapper failed' >&2\n"
        "exit 17\n"
    )
    failing_script.chmod(0o755)
    restart_env = {
        **os.environ,
        "BACKEND_PORT": "4111",
        "CORSIA_RESTART_BACKEND_PORT": "4111",
        "CORSIA_RESTART_OPERATION_ID": operation_id,
        "CORSIA_RESTART_CALLER_TOKEN": secret,
        "CORSIA_RESTART_CURL": str(fake_curl),
        "CORSIA_TOKEN": secret,
        "EXPECTED_OPERATION": operation_id,
        "EXPECTED_SECRET_FILE": str(expected_secret),
        "CURL_RESULT": str(curl_result),
        "CHILD_ENV": str(child_env),
        "_corsia_lifecycle_token": secret,
        "_corsia_cleanup_token": secret,
    }

    process = await _launch_settings_restart(
        operation_id,
        False,
        failing_script,
        restart_env,
        ["restart"],
        lambda name: tmp_path / name,
    )
    returncode = await asyncio.to_thread(process.wait, 5)

    assert returncode == 17
    assert curl_result.read_text().strip() == "ok:ok:unset:unset:unset:unset"
    assert "CORSIA_RESTART_CALLER_TOKEN=" not in child_env.read_text()
    assert secret not in child_env.read_text()
    assert secret not in (tmp_path / "restart.log").read_text()


@pytest.mark.asyncio
async def test_lifecycle_shell_ignores_hostile_inherited_bash_startup(
    tmp_path: Path,
    monkeypatch,
):
    """Inherited Bash hooks and functions cannot run before envelope traps."""
    from api.settings_restart import _launch_settings_restart

    sentinel = tmp_path / "startup-hook-ran"
    bash_env = tmp_path / "hostile-bash-env"
    bash_env.write_text(f"touch {sentinel}\nexit 91\n")
    script = tmp_path / "corsia"
    script.write_text("#!/bin/bash\nexit 0\n")
    script.chmod(0o755)
    fake_curl = tmp_path / "curl"
    fake_curl.write_text("#!/bin/bash\nexit 0\n")
    fake_curl.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: str(fake_curl))
    operation_id = "e" * 32
    restart_env = {
        **os.environ,
        "BACKEND_PORT": "4112",
        "CORSIA_RESTART_BACKEND_PORT": "4112",
        "CORSIA_RESTART_OPERATION_ID": operation_id,
        "CORSIA_RESTART_CALLER_TOKEN": "credential",
        "CORSIA_RESTART_CURL": str(fake_curl),
        "BASH_ENV": str(bash_env),
        "ENV": str(bash_env),
        "CDPATH": str(tmp_path),
        "SHELLOPTS": "xtrace",
        "BASHOPTS": "extdebug",
        "BASH_COMPAT": "42",
        "BASH_XTRACEFD": "9",
        "POSIXLY_CORRECT": "1",
        "PS4": "credential-trace ",
        "BASH_FUNC_source%%": "() { touch '" + str(sentinel) + "'; }",
    }

    process = await _launch_settings_restart(
        operation_id,
        False,
        script,
        restart_env,
        ["restart"],
        lambda name: tmp_path / name,
    )
    returncode = await asyncio.to_thread(process.wait, 5)

    assert returncode == 0
    assert not sentinel.exists()


def test_lifecycle_shell_envelope_is_static_and_syntax_valid():
    """The pre-trap owner is literal valid Bash with no request interpolation."""
    from api.restart_shell import LIFECYCLE_SHELL_COMMAND

    result = subprocess.run(
        ["/bin/bash", "-n"],
        input=LIFECYCLE_SHELL_COMMAND,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "dynamic-token-sentinel" not in LIFECYCLE_SHELL_COMMAND
    assert "c" * 32 not in LIFECYCLE_SHELL_COMMAND
    assert "4114" not in LIFECYCLE_SHELL_COMMAND
    assert LIFECYCLE_SHELL_COMMAND.index("set +x") < (
        LIFECYCLE_SHELL_COMMAND.index("trap _corsia_lifecycle_cleanup EXIT")
    )
    assert LIFECYCLE_SHELL_COMMAND.index("trap _corsia_lifecycle_cleanup EXIT") < (
        LIFECYCLE_SHELL_COMMAND.index("_corsia_lifecycle_token=")
    )


@pytest.mark.integration
class TestRestartInstanceIsolation:
    def test_api_token_cannot_restart(self, isolated_db):
        client = _client(3001)

        response = client.post(
            f"/api/system/restart?token={OWNER_TOKEN}&mode=update"
        )

        assert response.status_code == 403
        assert response.json()["detail"] == (
            "This change needs the signed-in person: open CORSIA and make it there."
        )

    def test_settings_restart_returns_and_owns_its_durable_operation(self, isolated_db):
        process = type("Process", (), {"wait": lambda self: None})()
        client = _client(3001)

        with patch("subprocess.Popen", return_value=process) as popen:
            response = client.post(
                f"/api/system/restart?token={_valid_token()}&mode=restart"
            )

        assert response.status_code == 200
        operation_id = response.json()["operation_id"]
        assert len(operation_id) == 32
        assert operation_id == operation_id.lower()
        assert set(operation_id) <= set("0123456789abcdef")
        row = _rows(
            "SELECT id, status, phase, requester_user, reason "
            "FROM self_restart_requests WHERE id=?",
            (operation_id,),
        )[0]
        assert row == {
            "id": operation_id,
            "status": "in_flight",
            "phase": "draining",
            "requester_user": "steve",
            "reason": "restart",
        }
        popen.assert_called_once()

    def test_restart_pins_own_port_not_default_port(self, isolated_db):
        client = _client(3001)
        with (
            patch(
                "api.settings_restart._prepare_current_release", return_value="-"
            ) as prepare,
            patch("subprocess.Popen") as popen,
        ):
            resp = client.post(
                f"/api/system/restart?token={_valid_token()}&mode=update"
            )
        assert resp.status_code == 200
        prepare.assert_called_once()
        _, kwargs = popen.call_args
        assert kwargs["env"]["BACKEND_PORT"] == "3001"
        assert kwargs["env"]["BACKEND_PORT"] != str(DEFAULT_BACKEND_PORT)

    def test_settings_restart_hands_the_exact_started_drain_to_the_shell(
        self, isolated_db
    ):
        process = type("Process", (), {"wait": lambda self: None})()
        client = _client(3001)

        with (
            patch(
                "automation.restart.begin_restart_drain",
                new_callable=AsyncMock,
                return_value={},
            ) as begin_drain,
            patch("subprocess.Popen", return_value=process) as popen,
        ):
            response = client.post(
                "/api/system/restart",
                params={"to" + "ken": _valid_token(), "mode": "restart"},
            )

        assert response.status_code == 200
        operation_id = response.json()["operation_id"]
        begin_drain.assert_awaited_once_with(
            "restart",
            operation_id=operation_id,
            allow_hard_cap=True,
        )
        _, kwargs = popen.call_args
        assert kwargs["env"]["CORSIA_RESTART_OPERATION_ID"] == operation_id

    def test_update_mode_prepares_then_launches_the_prepared_build(self, isolated_db):
        client = _client(3001)
        with (
            patch("api.settings_restart._prepare_current_release") as prepare,
            patch("subprocess.Popen") as popen,
        ):
            response = client.post(f"/api/system/restart?token={_valid_token()}&mode=update")
        prepare.assert_called_once()
        # An app bundle stages under the operation id, so prepare must carry it.
        prepare_env = prepare.call_args[0][1]
        assert prepare_env["CORSIA_RESTART_OPERATION_ID"] == response.json()["operation_id"]
        cmd = popen.call_args[0][0]
        assert "restart-prepared" in cmd
        assert "server" not in cmd

    def test_update_preflight_failure_releases_acquired_operation_without_bounce(
        self, isolated_db
    ):
        from fastapi import HTTPException

        client = _client(3001)
        with (
            patch(
                "api.settings_restart._prepare_current_release",
                side_effect=HTTPException(409, "pip check failed"),
            ),
            patch("subprocess.Popen") as popen,
        ):
            resp = client.post(
                f"/api/system/restart?token={_valid_token()}&mode=update"
            )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "pip check failed"
        popen.assert_not_called()
        assert _rows(
            "SELECT status, phase, error FROM self_restart_requests"
        ) == [{
            "status": "failed",
            "phase": None,
            "error": "pip check failed",
        }]

    def test_update_launch_failure_aborts_operation_and_retains_stage(self, isolated_db):
        client = _client(3001)
        with (
            patch("api.settings_restart._prepare_current_release"),
            patch("api.settings_restart._discard_prepared_release") as discard,
            patch("subprocess.Popen", side_effect=OSError("launch failed")),
        ):
            resp = client.post(
                "/api/system/restart",
                params={"to" + "ken": _valid_token(), "mode": "update"},
            )
        assert resp.status_code == 500
        assert resp.json()["detail"] == (
            "Update completed, but safe restart could not be started; "
            "CORSIA is still running."
        )
        discard.assert_not_called()
        rows = _rows("SELECT status, error FROM self_restart_requests")
        assert rows == [
            {
                "status": "failed",
                "error": "settings restart launch failed",
            }
        ]

    def test_invalid_shell_launch_tuple_compensates_before_handoff(
        self, isolated_db
    ):
        client = _client(3001)
        with (
            patch("api.restart_shell.shutil.which", return_value=None),
            patch("subprocess.Popen") as popen,
        ):
            response = client.post(
                "/api/system/restart",
                params={"to" + "ken": _valid_token(), "mode": "restart"},
            )

        assert response.status_code == 500
        popen.assert_not_called()
        assert _rows(
            "SELECT status,phase,error FROM self_restart_requests"
        ) == [{
            "status": "failed",
            "phase": None,
            "error": "settings restart launch failed",
        }]

    @pytest.mark.parametrize(
        "abort_outcome",
        [False, RuntimeError("restart database unavailable")],
        ids=["ownership-changed", "abort-raised"],
    )
    def test_update_launch_failure_surfaces_failed_compensation_and_retains_stage(
        self,
        isolated_db,
        abort_outcome,
    ):
        client = _client(3001)
        abort = AsyncMock()
        if isinstance(abort_outcome, Exception):
            abort.side_effect = abort_outcome
        else:
            abort.return_value = abort_outcome
        with (
            patch("api.settings_restart._prepare_current_release"),
            patch("api.settings_restart._discard_prepared_release") as discard,
            patch("automation.restart.abort_restart_control", abort),
            patch("subprocess.Popen", side_effect=OSError("launch failed")),
        ):
            response = client.post(
                "/api/system/restart",
                params={"to" + "ken": _valid_token(), "mode": "update"},
            )

        assert response.status_code == 500
        assert response.json()["detail"] == (
            "Update completed, but safe restart could not be started; "
            "CORSIA is still running. Restart control could not be released "
            "automatically; the active operation may still be holding ingress."
        )
        discard.assert_not_called()
        abort.assert_awaited_once()

    def test_settings_restart_rejects_the_existing_durable_operation(self, isolated_db):
        from automation import self_restart

        prior = self_restart.try_acquire(None, "steve", None, None, "prior restart")
        assert prior is not None
        client = _client(3001)

        with patch("subprocess.Popen") as popen:
            response = client.post(
                f"/api/system/restart?token={_valid_token()}&mode=restart"
            )

        assert response.status_code == 409
        assert _rows("SELECT id FROM self_restart_requests") == [{"id": prior}]
        popen.assert_not_called()

    def test_update_rejects_existing_operation_before_prepare(self, isolated_db):
        from automation import self_restart

        prior = self_restart.try_acquire(None, "steve", None, None, "prior restart")
        assert prior is not None
        client = _client(3001)

        with (
            patch("api.settings_restart._prepare_current_release") as prepare,
            patch("api.settings_restart._discard_prepared_release") as discard,
            patch("subprocess.Popen") as popen,
        ):
            response = client.post(
                f"/api/system/restart?token={_valid_token()}&mode=update"
            )

        assert response.status_code == 409
        assert response.json()["detail"] == "A restart is already in flight."
        assert _rows("SELECT id FROM self_restart_requests") == [{"id": prior}]
        prepare.assert_not_called()
        discard.assert_not_called()
        popen.assert_not_called()

    def test_restart_mode_never_pulls_or_builds(self, isolated_db):
        client = _client(3001)
        with (
            patch(
                "api.settings_restart._prepare_current_release", return_value="-"
            ) as prepare,
            patch("subprocess.Popen") as popen,
        ):
            resp = client.post(
                f"/api/system/restart?token={_valid_token()}&mode=restart"
            )
        assert resp.status_code == 200
        prepare.assert_not_called()
        cmd = popen.call_args[0][0]
        assert "restart" in cmd
        # CORSIA runs one backend, so the CLI takes no service selector.
        assert "server" not in cmd

    def test_restart_all_mode_is_rejected(self, isolated_db):
        """The restart-all mode promised a distinction it never implemented.

        It ran the same backend bounce as restart; only the argument
        differed. It is gone, so the endpoint must reject it rather than
        accept a mode that means nothing.
        """
        client = _client(3001)
        with patch("subprocess.Popen") as popen:
            resp = client.post(
                f"/api/system/restart?token={_valid_token()}&mode=restart-all"
            )
        assert resp.status_code == 400
        popen.assert_not_called()

    def test_restart_invalid_mode_rejected(self):
        client = _client(3001)
        with patch("subprocess.Popen") as popen:
            resp = client.post(f"/api/system/restart?token={_valid_token()}&mode=bogus")
        assert resp.status_code == 400
        popen.assert_not_called()

    def test_restart_requires_auth(self):
        client = _client(3001)
        with patch("subprocess.Popen") as popen:
            resp = client.post(
                "/api/system/restart?token=not-a-real-token&mode=restart"
            )
        assert resp.status_code == 401
        popen.assert_not_called()


# Hang guard only, not a latency requirement: before preparation starts the
# request crosses the DB pool and the drain, which take seconds on a loaded
# CI runner.
_PREPARE_WAIT_SECONDS = 15


@pytest.mark.asyncio
@pytest.mark.parametrize("current_version", ["1.1.0", "1.2.0"])
async def test_repeating_a_completed_packaged_update_returns_its_existing_outcome(isolated_db, monkeypatch, current_version):
    from starlette.requests import Request

    import api.system_admin as system_admin
    from automation import self_restart
    from infra.database import write_transaction
    from infra import update_status

    operation_id = await asyncio.to_thread(
        self_restart.try_acquire,
        None,
        "steve",
        None,
        None,
        "update",
        kind="update",
        trigger="manual",
        target_version="1.1.0",
    )
    assert operation_id is not None

    def complete() -> None:
        with write_transaction(None, scope="test") as connection:
            connection.execute(
                "UPDATE self_restart_requests SET status='complete',phase=NULL,settled_at='then' WHERE id=?",
                (operation_id,),
            )

    await asyncio.to_thread(complete)

    monkeypatch.setattr(
        update_status,
        "update_status",
        lambda _refresh: SimpleNamespace(current_version=current_version, target_version="1.1.0"),
    )
    launch = AsyncMock(return_value=("new-operation", object()))
    monkeypatch.setattr(system_admin, "launch_update", launch)

    result = await system_admin.system_restart(
        Request({"type": "http", "server": ("testserver", 3001)}), _valid_token(), mode="update"
    )

    assert result["operation_id"] == operation_id
    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_requests_are_single_flight_through_launch(isolated_db):
    from fastapi import HTTPException
    from starlette.requests import Request

    import api.system_admin as system_admin

    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def prepare(*_args):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=_PREPARE_WAIT_SECONDS)

    request = Request({"type": "http", "server": ("testserver", 3001)})
    with (
        patch("api.settings_restart._prepare_current_release", side_effect=prepare),
        patch("api.settings_restart._discard_prepared_release"),
        patch("subprocess.Popen"),
        patch("builtins.open", mock_open()),
    ):
        first = asyncio.create_task(
            system_admin.system_restart(request, _valid_token(), mode="update")
        )
        assert await asyncio.to_thread(entered.wait, _PREPARE_WAIT_SECONDS)
        second = asyncio.create_task(
            system_admin.system_restart(request, _valid_token(), mode="update")
        )
        await asyncio.sleep(0.05)
        assert calls == [1]
        release.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)

    assert calls == [1]
    assert outcomes[0]["ok"] is True
    assert isinstance(outcomes[1], HTTPException)
    assert outcomes[1].status_code == 409


@pytest.mark.asyncio
async def test_update_owns_restart_slot_through_preparation(isolated_db):
    from fastapi import HTTPException
    from starlette.requests import Request

    import api.system_admin as system_admin
    from automation import self_restart

    entered = threading.Event()
    release = threading.Event()

    def prepare(*_args):
        entered.set()
        assert release.wait(timeout=_PREPARE_WAIT_SECONDS)

    request = Request({"type": "http", "server": ("testserver", 3001)})
    begin_drain = AsyncMock()
    launch = AsyncMock(return_value=object())
    with (
        patch("api.settings_restart._prepare_current_release", side_effect=prepare),
        patch("api.settings_restart._begin_settings_drain", begin_drain),
        patch("api.settings_restart._launch_settings_restart", launch),
    ):
        update = asyncio.create_task(
            system_admin.system_restart(request, _valid_token(), mode="update")
        )
        assert await asyncio.to_thread(entered.wait, _PREPARE_WAIT_SECONDS)
        during_prepare = self_restart.in_flight_request()
        restart = asyncio.create_task(
            system_admin.system_restart(request, _valid_token(), mode="restart")
        )
        await asyncio.sleep(0.05)
        release.set()
        outcomes = await asyncio.gather(update, restart, return_exceptions=True)

    assert during_prepare is not None
    assert during_prepare["phase"] == "holding"
    assert outcomes[0]["ok"] is True
    assert isinstance(outcomes[1], HTTPException)
    assert outcomes[1].status_code == 409
    begin_drain.assert_awaited_once()
    assert begin_drain.await_args.args[1] == "update"
    launch.assert_awaited_once()
    assert launch.await_args.args[1] is True


@pytest.mark.asyncio
async def test_cancelled_update_retains_ownership_until_preparation_settles(
    isolated_db,
):
    from starlette.requests import Request

    import api.system_admin as system_admin
    from automation import self_restart

    entered = threading.Event()
    release = threading.Event()

    def prepare(*_args):
        entered.set()
        assert release.wait(timeout=_PREPARE_WAIT_SECONDS)

    request = Request({"type": "http", "server": ("testserver", 3001)})
    begin_drain = AsyncMock()
    launch = AsyncMock(return_value=object())
    with (
        patch("api.settings_restart._prepare_current_release", side_effect=prepare),
        patch("api.settings_restart._begin_settings_drain", begin_drain),
        patch("api.settings_restart._launch_settings_restart", launch),
    ):
        update = asyncio.create_task(
            system_admin.system_restart(request, _valid_token(), mode="update")
        )
        assert await asyncio.to_thread(entered.wait, _PREPARE_WAIT_SECONDS)
        operation = self_restart.in_flight_request()
        assert operation is not None
        assert operation["phase"] == "holding"

        update.cancel()
        await asyncio.sleep(0)
        assert not update.done()
        assert self_restart.try_acquire(
            None, "steve", None, None, "restart"
        ) is None

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await update

    assert _rows(
        "SELECT id, status, phase, error FROM self_restart_requests"
    ) == [{
        "id": operation["id"],
        "status": "cancelled",
        "phase": None,
        "error": "settings restart cancelled before launch",
    }]
    begin_drain.assert_not_awaited()
    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_drain_setup_failure_aborts_before_launch(isolated_db):
    from fastapi import HTTPException
    from starlette.requests import Request

    import api.system_admin as system_admin

    request = Request({"type": "http", "server": ("testserver", 3001)})
    with (
        patch(
            "infra.drain.start_drain_projection",
            side_effect=RuntimeError("drain unavailable"),
        ),
        patch("subprocess.Popen") as popen,
    ):
        with pytest.raises(
            HTTPException,
            match="Restart control could not be established",
        ) as error:
            await system_admin.system_restart(
                request,
                _valid_token(),
                mode="restart",
            )

    assert error.value.status_code == 409
    assert _rows("SELECT status, phase, error FROM self_restart_requests") == [
        {
            "status": "failed",
            "phase": None,
            "error": "restart control setup failed",
        }
    ]
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_restart_cancellation_during_drain_aborts_exact_operation(
    isolated_db,
):
    from starlette.requests import Request

    import api.system_admin as system_admin

    request = Request({"type": "http", "server": ("testserver", 3001)})
    with (
        patch("api.settings_restart._prepare_current_release"),
        patch(
            "api.settings_restart._begin_settings_drain",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError,
        ),
        patch("api.settings_restart._discard_prepared_release") as discard,
        patch("subprocess.Popen") as popen,
    ):
        with pytest.raises(asyncio.CancelledError):
            await system_admin.system_restart(
                request,
                _valid_token(),
                mode="update",
            )

    assert _rows("SELECT status, phase, error FROM self_restart_requests") == [
        {
            "status": "cancelled",
            "phase": None,
            "error": "settings restart cancelled before launch",
        }
    ]
    discard.assert_not_called()
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_restart_cancellation_after_committed_acquire_aborts_exact_operation(
    isolated_db,
    monkeypatch,
):
    from starlette.requests import Request

    import api.system_admin as system_admin
    from infra import db_async

    owner = asyncio.current_task()
    assert owner is not None
    real_run_in_db = db_async.run_in_db

    async def cancel_owner_after_acquire(function, *args, **kwargs):
        result = await real_run_in_db(function, *args, **kwargs)
        if getattr(function, "__name__", "") == "_try_acquire":
            owner.cancel()
            await asyncio.sleep(0)
        return result

    monkeypatch.setattr(db_async, "run_in_db", cancel_owner_after_acquire)
    request = Request({"type": "http", "server": ("testserver", 3001)})
    with (
        patch("api.settings_restart._prepare_current_release"),
        patch("api.settings_restart._discard_prepared_release") as discard,
        patch("subprocess.Popen") as popen,
    ):
        with pytest.raises(asyncio.CancelledError) as cancellation:
            await system_admin.system_restart(request, _valid_token(), mode="update")

    rows = _rows("SELECT status, phase, error FROM self_restart_requests")
    assert rows == [
        {
            "status": "cancelled",
            "phase": None,
            "error": "settings restart cancelled before launch",
        }
    ]
    discard.assert_not_called()
    popen.assert_not_called()
    assert not getattr(cancellation.value, "__notes__", [])


@pytest.mark.asyncio
async def test_update_acquire_database_failure_retains_stage_and_failure(
    isolated_db,
    monkeypatch,
):
    from starlette.requests import Request

    import api.system_admin as system_admin
    from infra import db_async

    async def fail_acquire(*_args, **_kwargs):
        raise sqlite3.OperationalError("restart database unavailable")

    monkeypatch.setattr(db_async, "run_in_db", fail_acquire)
    request = Request({"type": "http", "server": ("testserver", 3001)})
    with (
        patch("api.settings_restart._prepare_current_release"),
        patch("api.settings_restart._discard_prepared_release") as discard,
        patch("subprocess.Popen") as popen,
    ):
        with pytest.raises(
            sqlite3.OperationalError,
            match="restart database unavailable",
        ) as error:
            await system_admin.system_restart(request, _valid_token(), mode="update")

    discard.assert_not_called()
    popen.assert_not_called()
    assert not getattr(error.value, "__notes__", [])


@pytest.mark.asyncio
async def test_restart_cancellation_preserves_cancellation_when_abort_fails(
    isolated_db,
    monkeypatch,
):
    from starlette.requests import Request

    import api.system_admin as system_admin
    from infra import db_async

    owner = asyncio.current_task()
    assert owner is not None
    real_run_in_db = db_async.run_in_db

    async def cancel_owner_after_acquire(function, *args, **kwargs):
        result = await real_run_in_db(function, *args, **kwargs)
        if getattr(function, "__name__", "") == "_try_acquire":
            owner.cancel()
            await asyncio.sleep(0)
        return result

    monkeypatch.setattr(db_async, "run_in_db", cancel_owner_after_acquire)
    request = Request({"type": "http", "server": ("testserver", 3001)})
    with (
        patch(
            "automation.restart.abort_restart_control",
            side_effect=RuntimeError("abort failed"),
        ),
        patch("subprocess.Popen") as popen,
    ):
        with pytest.raises(asyncio.CancelledError) as cancellation:
            await system_admin.system_restart(request, _valid_token(), mode="restart")

    assert cancellation.value.__notes__ == [
        "Restart cancellation compensation failed: abort failed",
    ]
    popen.assert_not_called()


def test_update_log_paths_are_request_correlated(tmp_path: Path):
    import api.settings_restart as settings_restart

    with patch("infra.config.LOGS_DIR", tmp_path):
        first = settings_restart._update_log_path()
        second = settings_restart._update_log_path()

    assert first.parent == tmp_path
    assert second.parent == tmp_path
    assert first != second
    assert first.name.startswith("update-")
    assert second.name.startswith("update-")


def test_prepare_current_release_runs_current_checkout_preflight(tmp_path: Path):
    import subprocess

    import api.settings_restart as settings_restart

    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="Pull + dependency check + build OK", stderr=""
    )
    script = Path("/tmp/serving/scripts/corsia")
    with (
        patch("subprocess.run", return_value=completed) as run,
        patch(
            "api.settings_restart._update_log_path",
            return_value=tmp_path / "update.log",
        ),
    ):
        settings_restart._prepare_current_release(script, {"BACKEND_PORT": "3001"})

    run.assert_called_once_with(
        [str(script), "prepare-redeploy"],
        capture_output=True,
        text=True,
        env={"BACKEND_PORT": "3001"},
        timeout=600,
        check=False,
    )


def test_prepare_current_release_returns_bounded_terminal_failure_with_full_log(
    tmp_path: Path,
):
    import subprocess

    from fastapi import HTTPException

    import api.settings_restart as settings_restart

    branch_noise = "\n".join(
        f" * [new branch] fix/noise-{index} -> origin/fix/noise-{index}"
        for index in range(80)
    )
    terminal_error = (
        "error: Unable to create '/tmp/serving/.git/index.lock': File exists.\n"
        "Another git process seems to be running in this repository."
    )
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout=branch_noise,
        stderr=terminal_error,
    )
    log_path = tmp_path / "update.log"
    with (
        patch("subprocess.run", return_value=completed),
        patch("api.settings_restart._update_log_path", return_value=log_path),
    ):
        with pytest.raises(HTTPException) as exc:
            settings_restart._prepare_current_release(Path("/tmp/corsia"), {})

    assert exc.value.status_code == 409
    assert exc.value.detail.startswith(
        "Update stopped before restart: error: Unable to create "
    )
    assert "new branch" not in exc.value.detail
    assert str(log_path) in exc.value.detail
    assert len(exc.value.detail) < 500
    assert branch_noise in log_path.read_text()
    assert terminal_error in log_path.read_text()
    assert "\x1b" not in exc.value.detail


@pytest.mark.parametrize(
    ("returncode", "failure_code"),
    [
        (41, "fetch_failed"), (42, "build_failed"), (43, "index_locked"), (44, "checkout_detached"),
        (45, "checkout_dirty"), (46, "restart_pending"), (99, "prepare_failed"),
    ],
)
def test_prepare_current_release_names_every_refusal_it_knows(
    tmp_path: Path, returncode: int, failure_code: str | None,
):
    """Each explicit shell status is its own durable cause; an unknown one is still named."""
    import subprocess

    from fastapi import HTTPException

    import api.settings_restart as settings_restart

    completed = subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="failed")
    with (
        patch("subprocess.run", return_value=completed),
        patch("api.settings_restart._update_log_path", return_value=tmp_path / "update.log"),
        pytest.raises(HTTPException) as raised,
    ):
        settings_restart._prepare_current_release(Path("/tmp/corsia"), {})

    assert getattr(raised.value, "update_failure_code", None) == failure_code


def test_prepare_current_release_timeout_is_its_own_cause(tmp_path: Path):
    """A preparation timeout names itself instead of falling back to generic copy."""
    import subprocess

    from fastapi import HTTPException

    import api.settings_restart as settings_restart

    with (
        patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["corsia"], 600)),
        patch("api.settings_restart._update_log_path", return_value=tmp_path / "update.log"),
        pytest.raises(HTTPException) as raised,
    ):
        settings_restart._prepare_current_release(Path("/tmp/corsia"), {})

    assert getattr(raised.value, "update_failure_code", None) == "update_timed_out"


def _corsia_script_text() -> str:
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    return "\n".join(
        [
            (scripts_dir / "corsia").read_text(),
            *[
                path.read_text()
                for path in sorted((scripts_dir / "corsia.d").glob("*.sh"))
            ],
        ]
    )


def test_script_backend_discovery_is_port_scoped():
    text = _corsia_script_text()
    assert "lsof -nP -iTCP:" in text
    assert 'pgrep -f "uvicorn main:app' not in text


def test_script_relaunch_pins_backend_port():
    text = _corsia_script_text()
    assert 'BACKEND_PORT="$BACKEND_PORT" \\\n        nohup' in text
