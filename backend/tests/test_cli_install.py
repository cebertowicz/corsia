"""The in-app CLI installer starts one safe job per supported harness."""

from __future__ import annotations

import asyncio
from email.message import Message
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.error

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_jobs():
    """Keep in-memory installation state independent between tests."""
    from api import cli_install

    cli_install._jobs.clear()
    yield
    cli_install._jobs.clear()


class _Response:
    def __init__(self, content: bytes):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        return self.content


class _FakePopen:
    """A launched installer that answers `communicate` the way `subprocess.Popen` does, with no process behind it."""

    def __init__(self, command, observed, *, returncode=0, output=(b"", b""), timeout=False, on_communicate=None):
        self.args, self.pid, self.returncode = command, 0, None
        self._observed, self._final, self._output = observed, returncode, output
        self._timeout, self._on_communicate = timeout, on_communicate

    def communicate(self, timeout=None):
        self._observed["timeout"] = timeout
        if self._timeout and timeout is not None:
            raise subprocess.TimeoutExpired("bash", timeout)
        if self._on_communicate:
            self._on_communicate()
        self.returncode = self._final
        return self._output

    def poll(self):
        return self.returncode


def _client() -> TestClient:
    from api import cli_install

    app = FastAPI()
    app.include_router(cli_install.router)
    app.dependency_overrides[cli_install.require_user] = lambda: {"id": "user"}
    return TestClient(app)


@pytest.mark.parametrize(("error", "reason"), [
    (urllib.error.URLError("offline"), "no internet connection"),
    (urllib.error.HTTPError("https:" "//example.test", 403, "forbidden", Message(), None), "the download site refused the request"),
    (urllib.error.HTTPError("https:" "//example.test", 404, "missing", Message(), None), "the download site refused the request"),
    (urllib.error.HTTPError("https:" "//example.test", 408, "timeout", Message(), None), "the download service cannot answer"),
    (urllib.error.HTTPError("https:" "//example.test", 429, "limited", Message(), None), "the download service cannot answer"),
    (urllib.error.HTTPError("https:" "//example.test", 503, "down", Message(), None), "the download service cannot answer"),
])
def test_download_connection_failures_have_safe_reasons(monkeypatch, error, reason):
    from api import cli_install

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(cli_install.urllib.request, "urlopen", fail)
    with pytest.raises(cli_install.InstallerFailure, match=reason):
        cli_install._download_script("codex")


def test_download_request_identifies_corsia_with_its_app_version(monkeypatch):
    from api import cli_install

    observed = {}

    def fetch(request, **_kwargs):
        observed["user_agent"] = request.get_header("User-agent")
        return _Response(b"#!/bin/sh\n")

    monkeypatch.setattr(cli_install.build_info, "frontend_build_info", lambda: {"app": {"version": "0.4.1-dev.246"}})
    monkeypatch.setattr(cli_install.urllib.request, "urlopen", fetch)
    script = cli_install._download_script("codex")
    assert observed["user_agent"] == "CORSIA/0.4.1-dev.246"
    script.unlink()


def test_download_rejects_non_script_response(monkeypatch):
    from api import cli_install

    monkeypatch.setattr(cli_install.urllib.request, "urlopen", lambda *_a, **_k: _Response(b"<html>no</html>"))
    with pytest.raises(cli_install.InstallerFailure, match="the download didn't verify"):
        cli_install._download_script("claude-code")


def test_success_runs_bash_with_the_service_environment(monkeypatch, tmp_path):
    from api import cli_install

    script = tmp_path / "installer.sh"
    script.write_text("#!/bin/bash\n")
    observed = {}
    monkeypatch.setattr(cli_install, "_download_script", lambda _vendor: script)

    def popen(command, **kwargs):
        observed.update(command=command, **kwargs)
        return _FakePopen(command, observed)

    monkeypatch.setattr(cli_install.subprocess, "Popen", popen)
    assert cli_install._run_installer("codex", cli_install.InstallJob()) is None
    assert observed["command"] == ["bash", str(script)]
    assert observed["env"]["PATH"] == cli_install.service_path()
    assert observed["env"]["CODEX_NON_INTERACTIVE"] == "1"
    assert observed["timeout"] == 180 and observed["stdin"] is subprocess.DEVNULL
    assert observed["start_new_session"] is True
    assert not script.exists()


@pytest.mark.parametrize(("vendor", "result", "reason"), [
    ("codex", {"timeout": True}, "it took too long"),
    ("codex", {"returncode": 1, "output": (b"installer output", b"failed")}, "the installer stopped"),
    ("claude-code", {"returncode": 1, "output": (b"Checksum verification failed", b"")}, "the download didn't verify"),
    ("claude-code", {"returncode": 1, "output": (b"Failed to get a valid version", b"")}, "the download service cannot answer"),
    ("codex", {"returncode": 1, "output": (b"Could not fetch installer", b"")}, "no internet connection"),
])
def test_installer_failures_have_safe_reasons(monkeypatch, tmp_path, vendor, result, reason):
    from api import cli_install

    script = tmp_path / "installer.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(cli_install, "_download_script", lambda _vendor: script)

    monkeypatch.setattr(cli_install, "_stop_group", lambda _process: None)
    monkeypatch.setattr(cli_install.subprocess, "Popen", lambda command, **_kwargs: _FakePopen(command, {}, **result))
    assert cli_install._run_installer(vendor, cli_install.InstallJob()) == reason
    assert not script.exists()


@pytest.mark.asyncio
async def test_success_refreshes_sources_and_notifies_models(monkeypatch):
    from api import cli_install

    refreshed, notified = [], []

    async def refresh():
        refreshed.append(True)

    monkeypatch.setattr(cli_install, "_run_installer", lambda _vendor, _job: None)
    _nothing_installed(monkeypatch)
    monkeypatch.setattr(cli_install, "recheck_sources_once", refresh)
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: notified.append(True))
    monkeypatch.setattr(cli_install, "path_shadowed_binary", lambda _vendor: "/usr/local/bin/codex")
    assert await cli_install.start_install("codex", {}) == {"state": "running", "reason": None, "ref": None, "path_shadowed_by": None}
    await cli_install._jobs["codex"].task
    assert await cli_install.get_install("codex", {}) == {"state": "done", "reason": None, "ref": None, "path_shadowed_by": "/usr/local/bin/codex"}
    assert refreshed == [True] and notified == [True]


@pytest.mark.asyncio
async def test_done_is_reported_only_after_the_source_recheck(monkeypatch):
    """A reader that sees done must find the installed CLI in the sources, never the old not_installed."""
    from api import cli_install

    seen = []

    async def refresh():
        seen.append(cli_install._status("codex")["state"])

    monkeypatch.setattr(cli_install, "_run_installer", lambda _vendor, _job: None)
    _nothing_installed(monkeypatch)
    monkeypatch.setattr(cli_install, "recheck_sources_once", refresh)
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: None)
    monkeypatch.setattr(cli_install, "path_shadowed_binary", lambda _vendor: None)
    await cli_install.start_install("codex", {})
    await cli_install._jobs["codex"].task
    assert seen == ["running"]
    assert cli_install._status("codex")["state"] == "done"


@pytest.mark.asyncio
async def test_location_check_failure_does_not_break_a_successful_install(monkeypatch, caplog):
    from api import cli_install

    monkeypatch.setattr(cli_install, "_run_installer", lambda _vendor, _job: None)
    monkeypatch.setattr(cli_install, "path_shadowed_binary", lambda _vendor: (_ for _ in ()).throw(OSError("unreadable")))
    _nothing_installed(monkeypatch)
    monkeypatch.setattr(cli_install, "recheck_sources_once", lambda: asyncio.sleep(0))
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: None)

    with caplog.at_level("WARNING", logger=cli_install.log.name):
        await cli_install.start_install("codex", {})
        await cli_install._jobs["codex"].task
    assert await cli_install.get_install("codex", {}) == {"state": "done", "reason": None, "ref": None, "path_shadowed_by": None}
    assert "location check after installation did not complete" in caplog.text


@pytest.mark.asyncio
async def test_second_post_returns_the_existing_running_job(monkeypatch):
    from api import cli_install

    release = threading.Event()

    def run(_vendor, _job):
        release.wait()
        return None

    monkeypatch.setattr(cli_install, "_run_installer", run)
    _nothing_installed(monkeypatch)
    monkeypatch.setattr(cli_install, "recheck_sources_once", lambda: asyncio.sleep(0))
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: None)
    await cli_install.start_install("claude-code", {})
    assert await cli_install.start_install("claude-code", {}) == {"state": "running", "reason": None, "ref": None, "path_shadowed_by": None}
    release.set()
    await cli_install._jobs["claude-code"].task


@pytest.mark.asyncio
async def test_update_reuses_the_installer_unless_a_package_manager_owns_the_cli(monkeypatch):
    from api import cli_install

    refreshed = []
    monkeypatch.setattr(cli_install, "service_binary", lambda _vendor: "/usr/local/bin/codex")
    monkeypatch.setattr(cli_install, "package_manager_for", lambda *_args: None)
    monkeypatch.setattr(cli_install, "path_shadowed_binary", lambda _vendor: None)
    monkeypatch.setattr(cli_install, "_run_installer", lambda _vendor, _job: None)
    monkeypatch.setattr(cli_install, "recheck_sources_once", lambda: refreshed.append(True) or asyncio.sleep(0))
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: None)

    assert await cli_install.start_update("codex", {}) == {"state": "running", "reason": None, "ref": None, "path_shadowed_by": None}
    await cli_install._jobs["codex"].task
    assert refreshed == [True]

    cli_install._jobs.clear()
    monkeypatch.setattr(cli_install, "package_manager_for", lambda *_args: "npm")
    response = await cli_install.start_update("codex", {})
    assert response.status_code == 409 and response.body == b'{"reason":"package_managed"}'
    assert "codex" not in cli_install._jobs


def _nothing_installed(monkeypatch):
    """Neither the service PATH nor the login shell knows the CLI."""
    from api import cli_install

    monkeypatch.setattr(cli_install, "service_binary", lambda _vendor: None)
    monkeypatch.setattr(cli_install, "login_shell_binary", lambda _vendor: None)


@pytest.mark.asyncio
async def test_post_rejects_invisible_and_installed_binaries(monkeypatch):
    from api import cli_install

    monkeypatch.setattr(cli_install, "service_binary", lambda _vendor: None)
    monkeypatch.setattr(cli_install, "login_shell_binary", lambda _vendor: Path("/Users/user/bin/codex"))
    response = await cli_install.start_install("codex", {})
    assert response.status_code == 409 and response.body == b'{"reason":"invisible_binary"}'
    monkeypatch.setattr(cli_install, "service_binary", lambda _vendor: "/usr/local/bin/codex")
    response = await cli_install.start_install("codex", {})
    assert response.status_code == 409 and response.body == b'{"reason":"already_installed"}'


@pytest.mark.asyncio
async def test_guard_finds_a_login_shell_binary_live_with_an_empty_cache(monkeypatch, tmp_path):
    """A restarted server has no cached observation; the guard still sees a shell-only CLI."""
    from api import cli_install, provider_cli_link
    import cli_location

    binary = tmp_path / "login-bin" / "codex"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    provider_cli_link._invisible_binaries.clear()
    monkeypatch.setattr(cli_location.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("CORSIA_SERVICE_PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(cli_location, "shell_binary", lambda _name: str(binary))
    response = await cli_install.start_install("codex", {})
    assert response.status_code == 409 and response.body == b'{"reason":"invisible_binary"}'
    assert "codex" not in cli_install._jobs


@pytest.mark.asyncio
async def test_guard_treats_a_hung_login_shell_as_not_found(monkeypatch):
    from api import cli_install

    def hang(_vendor):
        raise TimeoutError

    monkeypatch.setattr(cli_install, "service_binary", lambda _vendor: None)
    monkeypatch.setattr(cli_install, "login_shell_binary", hang)
    monkeypatch.setattr(cli_install, "_run_installer", lambda _vendor, _job: None)
    monkeypatch.setattr(cli_install, "recheck_sources_once", lambda: asyncio.sleep(0))
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: None)
    assert await cli_install.start_install("codex", {}) == {"state": "running", "reason": None, "ref": None, "path_shadowed_by": None}
    await cli_install._jobs["codex"].task


@pytest.mark.asyncio
async def test_a_tempfile_error_settles_the_job_failed(monkeypatch):
    """The temp-file write is inside the failure handling: no job is left running."""
    from api import cli_install

    def broken(*_args, **_kwargs):
        raise OSError("no space left")

    monkeypatch.setattr(cli_install.urllib.request, "urlopen", lambda *_a, **_k: _Response(b"#!/bin/sh\n"))
    monkeypatch.setattr(cli_install.tempfile, "NamedTemporaryFile", broken)
    _nothing_installed(monkeypatch)
    monkeypatch.setattr(cli_install, "recheck_sources_once", lambda: asyncio.sleep(0))
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: None)
    await cli_install.start_install("codex", {})
    await cli_install._jobs["codex"].task
    result = await cli_install.get_install("codex", {})
    assert result["state"] == "failed" and result["reason"] == cli_install.INSTALLER_STOPPED
    assert result["ref"].startswith("cli-") and result["path_shadowed_by"] is None


@pytest.mark.asyncio
async def test_any_unexpected_error_settles_the_job_failed_and_is_logged(monkeypatch, caplog):
    from api import cli_install

    def explode(_vendor, _job):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli_install, "_run_installer", explode)
    _nothing_installed(monkeypatch)
    monkeypatch.setattr(cli_install, "recheck_sources_once", lambda: asyncio.sleep(0))
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: None)
    with caplog.at_level("ERROR", logger=cli_install.log.name):
        await cli_install.start_install("claude-code", {})
        await cli_install._jobs["claude-code"].task
    result = await cli_install.get_install("claude-code", {})
    assert result["state"] == "failed" and result["reason"] == cli_install.INSTALLER_STOPPED
    assert result["ref"].startswith("cli-") and result["path_shadowed_by"] is None
    assert "stopped unexpectedly" in caplog.text


@pytest.mark.parametrize(("reason", "cause"), [
    ("no internet connection", "connection"),
    ("the download site refused the request", "site_refused"),
    ("the download didn't verify", "download_unverified"),
    ("the installer stopped", "installer_failed"),
])
def test_install_failure_codes_are_independent_of_copy(reason, cause):
    from api import cli_install

    assert cli_install.install_cause(reason) == cause


def test_installer_env_passes_only_the_minimal_variables(monkeypatch):
    from api import cli_install

    for name in ("HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL"):
        monkeypatch.setenv(name, f"{name.lower()}-value")
    monkeypatch.setenv("CORSIA_API_TOKEN", "secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-value")
    env = cli_install._installer_env()
    assert set(env) == {"HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "PATH", "CODEX_NON_INTERACTIVE"}
    assert env["PATH"] == cli_install.service_path() and env["CODEX_NON_INTERACTIVE"] == "1"
    assert "secret-value" not in env.values()


def test_installer_env_omits_unset_variables(monkeypatch):
    from api import cli_install

    for name in ("USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL"):
        monkeypatch.delenv(name, raising=False)
    assert set(cli_install._installer_env()) <= {"HOME", "SHELL", "PATH", "CODEX_NON_INTERACTIVE"}


def test_get_is_exact_and_unknown_vendor_is_not_found():
    from api import cli_install

    with _client() as client:
        response = client.get("/api/providers/cli/codex/install")
        assert response.json() == {"state": "idle", "reason": None, "ref": None, "path_shadowed_by": None}
    with pytest.raises(HTTPException, match="CLI vendor not found"):
        asyncio.run(cli_install.get_install("unknown", {}))


# Cancel. The first three run a real installer process: a script that starts a child and waits on it.

_STUBBORN_INSTALLER = """#!/bin/bash
{trap}
echo $$ > "{pids}/installer"
sleep 60 &
echo $! > "{pids}/child"
wait
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _gone_soon(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


async def _start_real_installer(monkeypatch, tmp_path, *, trap: str = ""):
    """Start a job whose installer is a real shell script; returns the script path and the pid directory."""
    from api import cli_install

    script = tmp_path / "installer.sh"
    script.write_text(_STUBBORN_INSTALLER.format(trap=trap, pids=tmp_path))
    monkeypatch.setattr(cli_install, "_download_script", lambda _vendor: script)
    monkeypatch.setattr(cli_install, "recheck_sources_once", lambda: asyncio.sleep(0))
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: None)
    _nothing_installed(monkeypatch)
    await cli_install.start_install("claude-code", {})
    deadline = time.monotonic() + 5
    while not (tmp_path / "child").exists() or not (tmp_path / "child").read_text().strip():
        assert time.monotonic() < deadline, "the installer never started"
        await asyncio.sleep(0.05)
    return script, tmp_path


@pytest.mark.asyncio
async def test_cancel_while_running_stops_the_installer_tree_and_settles_cancelled(monkeypatch, tmp_path):
    from api import cli_install

    script, pids = await _start_real_installer(monkeypatch, tmp_path)
    installer, child = int((pids / "installer").read_text()), int((pids / "child").read_text())
    assert _alive(installer) and _alive(child)

    assert await cli_install.cancel_install("claude-code", {}) == {"state": "cancelled", "reason": None, "ref": None, "path_shadowed_by": None}
    await cli_install._jobs["claude-code"].task

    assert _gone_soon(installer) and _gone_soon(child)
    assert not script.exists()
    assert await cli_install.get_install("claude-code", {}) == {"state": "cancelled", "reason": None, "ref": None, "path_shadowed_by": None}


@pytest.mark.asyncio
async def test_cancel_kills_an_installer_that_ignores_the_first_signal(monkeypatch, tmp_path):
    from api import cli_install

    monkeypatch.setattr(cli_install, "_CANCEL_GRACE_SECONDS", 0.3)
    script, pids = await _start_real_installer(monkeypatch, tmp_path, trap="trap '' TERM")
    installer, child = int((pids / "installer").read_text()), int((pids / "child").read_text())

    assert (await cli_install.cancel_install("claude-code", {}))["state"] == "cancelled"
    await cli_install._jobs["claude-code"].task

    assert _gone_soon(installer) and _gone_soon(child)
    assert not script.exists()


@pytest.mark.asyncio
async def test_a_new_install_can_start_after_a_cancel(monkeypatch, tmp_path):
    from api import cli_install

    await _start_real_installer(monkeypatch, tmp_path)
    await cli_install.cancel_install("claude-code", {})
    await cli_install._jobs["claude-code"].task
    assert cli_install._jobs["claude-code"].state == "cancelled"

    (tmp_path / "child").unlink()
    assert await cli_install.start_install("claude-code", {}) == {"state": "running", "reason": None, "ref": None, "path_shadowed_by": None}
    await cli_install.cancel_install("claude-code", {})
    await cli_install._jobs["claude-code"].task


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["done", "failed", "cancelled"])
async def test_cancel_leaves_a_settled_job_alone(state):
    from api import cli_install

    job = cli_install.InstallJob(state=state, reason="it took too long" if state == "failed" else None, path_shadowed_by="/usr/bin/codex" if state == "done" else None)
    cli_install._jobs["codex"] = job
    before = cli_install._status("codex")
    assert await cli_install.cancel_install("codex", {}) == before
    assert job.state == state and not job.cancel_requested


def test_cancel_when_nothing_ran_is_a_no_op_over_http_and_unknown_vendors_are_not_found():
    from api import cli_install

    with _client() as client:
        response = client.delete("/api/providers/cli/codex/install")
        assert response.status_code == 200
        assert response.json() == {"state": "idle", "reason": None, "ref": None, "path_shadowed_by": None}
        assert client.delete("/api/providers/cli/unknown/install").status_code == 404


def test_a_cancel_before_the_installer_launches_means_it_never_launches(monkeypatch, tmp_path):
    from api import cli_install

    script = tmp_path / "installer.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(cli_install, "_download_script", lambda _vendor: script)

    def launched(*_args, **_kwargs):
        raise AssertionError("a cancelled job must not launch the installer")

    monkeypatch.setattr(cli_install.subprocess, "Popen", launched)
    job = cli_install.InstallJob()
    cli_install._request_cancel(job)
    assert cli_install._run_installer("codex", job) is None
    assert job.cancelled and not script.exists()


def test_a_cancel_that_arrives_after_a_successful_install_leaves_it_done(monkeypatch, tmp_path):
    """The installer finished: the CLI is there, so the one final state is done, not cancelled."""
    from api import cli_install

    script = tmp_path / "installer.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(cli_install, "_download_script", lambda _vendor: script)
    job = cli_install.InstallJob()

    def cancel_now():
        job.cancel_requested = True

    monkeypatch.setattr(cli_install.subprocess, "Popen", lambda command, **_kwargs: _FakePopen(command, {}, returncode=0, on_communicate=cancel_now))
    assert cli_install._run_installer("codex", job) is None
    assert not job.cancelled and not script.exists()


def test_a_cancel_that_arrives_while_the_installer_runs_settles_cancelled_not_failed(monkeypatch, tmp_path):
    from api import cli_install

    script = tmp_path / "installer.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(cli_install, "_download_script", lambda _vendor: script)
    job = cli_install.InstallJob()

    def cancel_now():
        job.cancel_requested = True

    monkeypatch.setattr(cli_install.subprocess, "Popen", lambda command, **_kwargs: _FakePopen(command, {}, returncode=-15, output=(b"", b"Terminated"), on_communicate=cancel_now))
    assert cli_install._run_installer("codex", job) is None
    assert job.cancelled


@pytest.mark.asyncio
async def test_a_cancel_racing_completion_returns_the_single_state_the_job_settled_in(monkeypatch):
    from api import cli_install

    entered, release = threading.Event(), threading.Event()

    def finish_first(_vendor, _job):
        entered.set()
        release.wait()
        return None

    monkeypatch.setattr(cli_install, "_run_installer", finish_first)
    monkeypatch.setattr(cli_install, "path_shadowed_binary", lambda _vendor: None)
    monkeypatch.setattr(cli_install, "recheck_sources_once", lambda: asyncio.sleep(0))
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: None)
    _nothing_installed(monkeypatch)
    await cli_install.start_install("codex", {})
    assert await asyncio.to_thread(entered.wait, 5)

    cancelling = asyncio.create_task(cli_install.cancel_install("codex", {}))
    await asyncio.sleep(0.05)
    release.set()
    assert await cancelling == {"state": "done", "reason": None, "ref": None, "path_shadowed_by": None}
    await cli_install._jobs["codex"].task
    assert cli_install._status("codex")["state"] == "done"
