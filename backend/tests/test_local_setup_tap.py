"""Source setup installs missing oMLX and never modifies an existing installation."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from llm import managed_omlx_binary, local_setup_executable


@pytest.fixture(autouse=True)
def working_entrypoint(monkeypatch):
    monkeypatch.setattr(local_setup_executable, "_is_broken_leftover", lambda _path: False)


def test_source_install_verifies_the_shared_wheel_before_install(monkeypatch, tmp_path):
    from llm.omlx_release import OMLX_VERSION

    commands = []
    payload = b"wheel"
    environment = tmp_path / "runtime"
    monkeypatch.setattr(managed_omlx_binary, "source_omlx_environment", lambda: environment)
    monkeypatch.setattr(local_setup_executable, "OMLX_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(local_setup_executable.shutil, "which", lambda _name: None)

    def run(command):
        commands.append(command)
        if command[0] == "curl":
            Path(command[-1]).write_bytes(payload)
        elif command[1:3] == ["-m", "venv"]:
            environment.mkdir()

    monkeypatch.setattr(local_setup_executable, "run_command", run)
    local_setup_executable.ensure_omlx(lambda _stage: None)
    assert f"/v{OMLX_VERSION}/omlx-{OMLX_VERSION}-" in commands[0][-3]
    assert commands[2][1:4] == ["-m", "pip", "install"]
    assert (environment / ".installed").is_file()


def test_hash_mismatch_never_runs_install(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(managed_omlx_binary, "source_omlx_environment", lambda: tmp_path / "runtime")
    monkeypatch.setattr(local_setup_executable, "OMLX_SHA256", "wrong")

    def run(command):
        commands.append(command)
        Path(command[-1]).write_bytes(b"wheel")

    monkeypatch.setattr(local_setup_executable, "run_command", run)
    with pytest.raises(RuntimeError, match="SHA-256 check"):
        local_setup_executable.install_omlx()
    assert len(commands) == 1
    assert not (tmp_path / "runtime" / ".installed").exists()


def test_existing_brew_install_is_used_without_modification(monkeypatch):
    monkeypatch.setattr(managed_omlx_binary, "managed_omlx_executable", lambda: None)
    monkeypatch.setattr(local_setup_executable, "omlx_on_path", lambda: "/opt/homebrew/bin/omlx")
    monkeypatch.setattr(local_setup_executable, "install_omlx", lambda: pytest.fail("existing oMLX was replaced"))
    local_setup_executable.ensure_omlx(lambda _stage: None)


def test_pip_failure_keeps_its_own_stderr(monkeypatch, tmp_path):
    environment = tmp_path / "runtime"
    payload = b"wheel"
    monkeypatch.setattr(managed_omlx_binary, "source_omlx_environment", lambda: environment)
    monkeypatch.setattr(local_setup_executable, "OMLX_SHA256", hashlib.sha256(payload).hexdigest())

    def run(command):
        if command[0] == "curl":
            Path(command[-1]).write_bytes(payload)
        elif command[1:3] == ["-m", "venv"]:
            environment.mkdir()
        else:
            raise subprocess.CalledProcessError(1, command, stderr="first\nCould not install package\n")

    monkeypatch.setattr(local_setup_executable, "run_command", run)
    monkeypatch.setattr(managed_omlx_binary, "command_output", lambda _cmd: pytest.fail("unexpected follow-up probe"))
    with pytest.raises(RuntimeError, match="pip couldn't install oMLX: Could not install package"):
        local_setup_executable.install_omlx()
    assert not (environment / ".installed").exists()


def test_packaged_setup_uses_the_bundled_omlx_without_brew(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(managed_omlx_binary, "managed_omlx_executable", lambda: "/Applications/CORSIA.app/Contents/Resources/bin/omlx")
    monkeypatch.setattr(local_setup_executable, "run_command", commands.append)
    monkeypatch.setattr(local_setup_executable.shutil, "which", lambda _name: "/opt/homebrew/bin/omlx")
    local_setup_executable.ensure_omlx(lambda _stage: None, needs_repair=True)
    assert commands == []


def test_packaged_setup_reports_a_damaged_app_without_brew(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(managed_omlx_binary, "managed_omlx_executable", lambda: (_ for _ in ()).throw(
        RuntimeError("The CORSIA app is damaged: its bundled oMLX executable is missing.")))
    monkeypatch.setattr(local_setup_executable, "run_command", commands.append)
    with pytest.raises(RuntimeError, match="CORSIA app is damaged"):
        local_setup_executable.ensure_omlx(lambda _stage: None)
    assert commands == []


def test_bundled_omlx_on_path_is_not_the_persons_omlx(monkeypatch):
    bundled = "/Applications/CORSIA.app/Contents/Resources/bin/omlx"
    monkeypatch.setattr(local_setup_executable.shutil, "which", lambda _name: bundled)
    monkeypatch.setattr(managed_omlx_binary, "is_managed_omlx_executable", lambda path: path == bundled)
    assert local_setup_executable.omlx_on_path() is None


def test_persons_brew_omlx_stays_visible_when_the_bundle_exists(monkeypatch):
    binary = "/opt/homebrew/bin/omlx"
    monkeypatch.setattr(local_setup_executable.shutil, "which", lambda _name: binary)
    monkeypatch.setattr(managed_omlx_binary, "is_managed_omlx_executable", lambda _path: False)
    assert local_setup_executable.omlx_on_path() == binary


def test_homebrew_inspection_has_a_bounded_timeout(monkeypatch):
    seen: dict[str, object] = {}

    def completed(*_args, **kwargs):
        seen.update(kwargs)
        return type("Result", (), {"stdout": "omlx 0.7.0\n"})()

    monkeypatch.setattr(local_setup_executable.subprocess, "run", completed)
    assert managed_omlx_binary.command_output(["brew", "list", "--versions", "omlx"]) == "omlx 0.7.0"
    assert seen["timeout"] == 30


def test_existing_binary_repair_is_actionable_and_never_runs_brew(monkeypatch):
    monkeypatch.setattr(local_setup_executable.shutil, "which", lambda name: "/opt/bin/brew" if name == "brew" else "/opt/bin/omlx")
    monkeypatch.setattr(local_setup_executable, "run_command", lambda _cmd: pytest.fail("existing oMLX must stay untouched"))
    monkeypatch.setattr(managed_omlx_binary, "command_output", lambda _cmd: pytest.fail("never infer ownership from a brew keg"))
    with pytest.raises(local_setup_executable.OwnOmlxUpgradeRequired) as failure:
        local_setup_executable.ensure_omlx(lambda _stage: None, needs_repair=True)
    assert "brew upgrade jundot/omlx/omlx" in str(failure.value)
    assert "brew reinstall jundot/omlx/omlx" in str(failure.value)
    assert "install the CORSIA app to use its bundled oMLX" in str(failure.value)


@pytest.mark.parametrize("stderr,expected", [
    ("first failure\nlast failure", "curl couldn't download oMLX: last failure. Try again."),
    ("old detail\nremote differs", "curl couldn't download oMLX: remote differs. Try again."),
    ("", "curl couldn't download oMLX. Try again."),
])
def test_fresh_install_failure_has_bounded_tool_feedback(monkeypatch, stderr, expected):
    monkeypatch.setattr(local_setup_executable.shutil, "which", lambda _name: None)

    def failed(command):
        raise subprocess.CalledProcessError(1, command, stderr=stderr)

    monkeypatch.setattr(local_setup_executable, "run_command", failed)
    with pytest.raises(RuntimeError) as failure:
        local_setup_executable.ensure_omlx(lambda _stage: None)
    assert str(failure.value) == expected
    assert len(local_setup_executable.installer_failure_message("pip", "install", subprocess.CalledProcessError(1, [], stderr="x" * 500))) < 350
