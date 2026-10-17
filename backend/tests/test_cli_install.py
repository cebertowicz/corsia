"""The in-app CLI installer starts one safe job per supported harness."""

from __future__ import annotations

import asyncio
from email.message import Message
from pathlib import Path
import subprocess
import threading
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


def _client() -> TestClient:
    from api import cli_install

    app = FastAPI()
    app.include_router(cli_install.router)
    app.dependency_overrides[cli_install.require_user] = lambda: {"id": "user"}
    return TestClient(app)


@pytest.mark.parametrize(("error", "reason"), [
    (urllib.error.URLError("offline"), "no internet connection"),
    (urllib.error.HTTPError("https:" "//example.test", 503, "down", Message(), None), "the download service didn't answer"),
])
def test_download_connection_failures_have_safe_reasons(monkeypatch, error, reason):
    from api import cli_install

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(cli_install.urllib.request, "urlopen", fail)
    with pytest.raises(cli_install.InstallerFailure, match=reason):
        cli_install._download_script("codex")


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

    def run(command, **kwargs):
        observed.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(cli_install.subprocess, "run", run)
    assert cli_install._run_installer("codex") is None
    assert observed["command"] == ["bash", str(script)]
    assert observed["env"]["PATH"] == cli_install.service_path()
    assert observed["env"]["CODEX_NON_INTERACTIVE"] == "1"
    assert observed["timeout"] == 180 and observed["stdin"] is subprocess.DEVNULL
    assert not script.exists()


@pytest.mark.parametrize(("vendor", "result", "reason"), [
    ("codex", subprocess.TimeoutExpired("bash", 180), "it took too long"),
    ("codex", subprocess.CompletedProcess([], 1, b"installer output", b"failed"), "the installer stopped"),
    ("claude-code", subprocess.CompletedProcess([], 1, b"Checksum verification failed", b""), "the download didn't verify"),
    ("claude-code", subprocess.CompletedProcess([], 1, b"Failed to get a valid version", b""), "the download service didn't answer"),
    ("codex", subprocess.CompletedProcess([], 1, b"Could not fetch installer", b""), "no internet connection"),
])
def test_installer_failures_have_safe_reasons(monkeypatch, tmp_path, vendor, result, reason):
    from api import cli_install

    script = tmp_path / "installer.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(cli_install, "_download_script", lambda _vendor: script)

    def run(*_args, **_kwargs):
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(cli_install.subprocess, "run", run)
    assert cli_install._run_installer(vendor) == reason
    assert not script.exists()


@pytest.mark.asyncio
async def test_success_refreshes_sources_and_notifies_models(monkeypatch):
    from api import cli_install

    refreshed, notified = [], []

    async def refresh():
        refreshed.append(True)

    monkeypatch.setattr(cli_install, "_run_installer", lambda _vendor: None)
    _nothing_installed(monkeypatch)
    monkeypatch.setattr(cli_install, "recheck_sources_once", refresh)
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: notified.append(True))
    monkeypatch.setattr(cli_install, "path_shadowed_binary", lambda _vendor: "/usr/local/bin/codex")
    assert await cli_install.start_install("codex", {}) == {"state": "running", "reason": None, "path_shadowed_by": None}
    await cli_install._jobs["codex"].task
    assert await cli_install.get_install("codex", {}) == {"state": "done", "reason": None, "path_shadowed_by": "/usr/local/bin/codex"}
    assert refreshed == [True] and notified == [True]


@pytest.mark.asyncio
async def test_location_check_failure_does_not_break_a_successful_install(monkeypatch, caplog):
    from api import cli_install

    monkeypatch.setattr(cli_install, "_run_installer", lambda _vendor: None)
    monkeypatch.setattr(cli_install, "path_shadowed_binary", lambda _vendor: (_ for _ in ()).throw(OSError("unreadable")))
    _nothing_installed(monkeypatch)
    monkeypatch.setattr(cli_install, "recheck_sources_once", lambda: asyncio.sleep(0))
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: None)

    with caplog.at_level("WARNING", logger=cli_install.log.name):
        await cli_install.start_install("codex", {})
        await cli_install._jobs["codex"].task
    assert await cli_install.get_install("codex", {}) == {"state": "done", "reason": None, "path_shadowed_by": None}
    assert "location check after installation did not complete" in caplog.text


@pytest.mark.asyncio
async def test_second_post_returns_the_existing_running_job(monkeypatch):
    from api import cli_install

    release = threading.Event()

    def run(_vendor):
        release.wait()
        return None

    monkeypatch.setattr(cli_install, "_run_installer", run)
    _nothing_installed(monkeypatch)
    monkeypatch.setattr(cli_install, "recheck_sources_once", lambda: asyncio.sleep(0))
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: None)
    await cli_install.start_install("claude-code", {})
    assert await cli_install.start_install("claude-code", {}) == {"state": "running", "reason": None, "path_shadowed_by": None}
    release.set()
    await cli_install._jobs["claude-code"].task


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
    monkeypatch.setattr(cli_install, "_run_installer", lambda _vendor: None)
    monkeypatch.setattr(cli_install, "recheck_sources_once", lambda: asyncio.sleep(0))
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: None)
    assert await cli_install.start_install("codex", {}) == {"state": "running", "reason": None, "path_shadowed_by": None}
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
    assert await cli_install.get_install("codex", {}) == {"state": "failed", "reason": cli_install.INSTALLER_STOPPED, "path_shadowed_by": None}


@pytest.mark.asyncio
async def test_any_unexpected_error_settles_the_job_failed_and_is_logged(monkeypatch, caplog):
    from api import cli_install

    def explode(_vendor):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli_install, "_run_installer", explode)
    _nothing_installed(monkeypatch)
    monkeypatch.setattr(cli_install, "recheck_sources_once", lambda: asyncio.sleep(0))
    monkeypatch.setattr(cli_install, "schedule_models_changed", lambda: None)
    with caplog.at_level("ERROR", logger=cli_install.log.name):
        await cli_install.start_install("claude-code", {})
        await cli_install._jobs["claude-code"].task
    assert await cli_install.get_install("claude-code", {}) == {"state": "failed", "reason": cli_install.INSTALLER_STOPPED, "path_shadowed_by": None}
    assert "stopped unexpectedly" in caplog.text


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
        assert response.json() == {"state": "idle", "reason": None, "path_shadowed_by": None}
    with pytest.raises(HTTPException, match="CLI vendor not found"):
        asyncio.run(cli_install.get_install("unknown", {}))
