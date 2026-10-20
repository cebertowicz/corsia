"""Source setup installs missing oMLX and never modifies an existing installation."""

import subprocess

import pytest

from llm import managed_omlx_binary, local_setup_executable


@pytest.fixture(autouse=True)
def working_entrypoint(monkeypatch):
    monkeypatch.setattr(local_setup_executable, "_is_broken_leftover", lambda _path: False)


def test_the_tap_is_added_with_the_repository_url_before_the_install(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(local_setup_executable, "run_command", commands.append)
    monkeypatch.setattr(local_setup_executable.shutil, "which", lambda name: "/opt/bin/brew" if name == "brew" else None)
    local_setup_executable.ensure_omlx(lambda _stage: None)
    assert commands == [
        ["/opt/bin/brew", "tap", "jundot/omlx", "https://github.com/jundot/omlx"],
        ["/opt/bin/brew", "install", "jundot/omlx/omlx"],
    ]


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


@pytest.mark.parametrize("broken", [False, True])
def test_existing_binary_repair_is_actionable_and_never_runs_brew(monkeypatch, broken):
    monkeypatch.setattr(local_setup_executable.shutil, "which", lambda name: "/opt/bin/brew" if name == "brew" else "/opt/bin/omlx")
    monkeypatch.setattr(local_setup_executable, "run_command", lambda _cmd: pytest.fail("existing oMLX must stay untouched"))
    monkeypatch.setattr(managed_omlx_binary, "command_output", lambda _cmd: pytest.fail("never infer ownership from a brew keg"))
    with pytest.raises(local_setup_executable.OwnOmlxUpgradeRequired) as failure:
        local_setup_executable.ensure_omlx(lambda _stage: None, needs_repair=True, broken=broken)
    assert "brew upgrade jundot/omlx/omlx" in str(failure.value)
    assert "brew reinstall jundot/omlx/omlx" in str(failure.value)
    assert "install the CORSIA app to use its bundled oMLX" in str(failure.value)


@pytest.mark.parametrize("stderr,expected", [
    ("first failure\nlast failure", "Homebrew couldn't install oMLX: last failure. Try again."),
    ("old detail\ntap remote differs", "Homebrew couldn't install oMLX: tap remote differs. Try again."),
    ("", "Homebrew couldn't install oMLX. Try again."),
])
def test_fresh_install_failure_has_bounded_homebrew_feedback(monkeypatch, stderr, expected):
    monkeypatch.setattr(local_setup_executable.shutil, "which", lambda name: "/opt/bin/brew" if name == "brew" else None)

    def failed(command):
        raise subprocess.CalledProcessError(1, command, stderr=stderr)

    monkeypatch.setattr(local_setup_executable, "run_command", failed)
    with pytest.raises(RuntimeError) as failure:
        local_setup_executable.ensure_omlx(lambda _stage: None)
    assert str(failure.value) == expected
