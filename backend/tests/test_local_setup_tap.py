"""Local setup installs oMLX from its tap, which must be given the repository URL."""

import subprocess

import pytest

from llm import local_setup_ownership


def test_the_tap_is_added_with_the_repository_url_before_the_install(monkeypatch, tmp_path):
    commands: list[list[str]] = []
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: tmp_path / "marker")
    monkeypatch.setattr(local_setup_ownership, "run_command", commands.append)
    monkeypatch.setattr(local_setup_ownership.shutil, "which",
                        lambda name: "/opt/bin/brew" if name == "brew" else None)

    local_setup_ownership.ensure_omlx(lambda stage: None)

    assert commands == [
        ["/opt/bin/brew", "tap", "jundot/omlx", "https://github.com/jundot/omlx"],
        ["/opt/bin/brew", "install", "jundot/omlx/omlx"],
    ]


def test_homebrew_inspection_has_a_bounded_timeout(monkeypatch):
    seen: dict[str, object] = {}

    def completed(*_args, **kwargs):
        seen.update(kwargs)
        return type("Result", (), {"stdout": "omlx 0.7.0\n"})()

    monkeypatch.setattr(local_setup_ownership.subprocess, "run", completed)

    assert local_setup_ownership.command_output(["brew", "list", "--versions", "omlx"]) == "omlx 0.7.0"
    assert seen["timeout"] == 30


def test_an_outdated_omlx_owned_by_setup_refreshes_the_tap_then_upgrades(monkeypatch, tmp_path):
    commands: list[list[str]] = []
    marker = tmp_path / "marker"
    marker.write_text("installed by corsia local setup\n", encoding="utf-8")
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: marker)
    monkeypatch.setattr(local_setup_ownership, "run_command", commands.append)
    monkeypatch.setattr(local_setup_ownership.shutil, "which",
                        lambda name: "/opt/bin/brew" if name == "brew" else "/opt/bin/omlx")
    monkeypatch.setattr(local_setup_ownership, "setup_owns_current_omlx", lambda *_args: True,
                        raising=False)

    local_setup_ownership.ensure_omlx(lambda stage: None, needs_repair=True, broken=False)

    assert commands == [
        ["/opt/bin/brew", "tap", "jundot/omlx", "https://github.com/jundot/omlx"],
        ["/opt/bin/brew", "update"],
        ["/opt/bin/brew", "upgrade", "jundot/omlx/omlx"],
    ]


def test_a_broken_omlx_owned_by_setup_refreshes_the_tap_then_reinstalls(monkeypatch, tmp_path):
    commands: list[list[str]] = []
    marker = tmp_path / "marker"
    marker.write_text("installed by corsia local setup\n", encoding="utf-8")
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: marker)
    monkeypatch.setattr(local_setup_ownership, "run_command", commands.append)
    monkeypatch.setattr(local_setup_ownership.shutil, "which",
                        lambda name: "/opt/bin/brew" if name == "brew" else "/opt/bin/omlx")
    monkeypatch.setattr(local_setup_ownership, "setup_owns_current_omlx", lambda *_args: True,
                        raising=False)

    local_setup_ownership.ensure_omlx(lambda stage: None, needs_repair=True, broken=True)

    assert commands == [
        ["/opt/bin/brew", "tap", "jundot/omlx", "https://github.com/jundot/omlx"],
        ["/opt/bin/brew", "update"],
        ["/opt/bin/brew", "reinstall", "jundot/omlx/omlx"],
    ]


def test_a_stale_marker_does_not_authorize_repairing_a_different_omlx(monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("installed by corsia local setup\n", encoding="utf-8")
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: marker)
    monkeypatch.setattr(local_setup_ownership.shutil, "which",
                        lambda name: "/opt/bin/brew" if name == "brew" else "/usr/local/bin/omlx")
    monkeypatch.setattr(
        local_setup_ownership, "command_output",
        lambda command: "jundot/omlx 0.7.0" if command[1:3] == ["list", "--versions"] else "/opt/homebrew/opt/omlx",
        raising=False,
    )

    assert local_setup_ownership.existing_install_is_foreign() is True


@pytest.mark.parametrize(("broken", "action"), [(False, "update"), (True, "reinstall")])
def test_a_failed_owned_omlx_repair_names_only_the_last_homebrew_stderr_line(
        monkeypatch, tmp_path, broken, action):
    marker = tmp_path / "marker"
    marker.write_text("installed by corsia local setup\n", encoding="utf-8")
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: marker)
    monkeypatch.setattr(local_setup_ownership.shutil, "which",
                        lambda name: "/opt/bin/brew" if name == "brew" else "/opt/bin/omlx")

    def failed(command):
        if command[1] == "tap" or (command[1] == "update" and action == "reinstall"):
            return None
        raise subprocess.CalledProcessError(1, command, stderr="first failure\nlast failure")

    monkeypatch.setattr(local_setup_ownership, "run_command", failed)
    monkeypatch.setattr(local_setup_ownership, "setup_owns_current_omlx", lambda *_args: True,
                        raising=False)

    with pytest.raises(RuntimeError, match=rf"Homebrew couldn't {action} oMLX: last failure\. Try again\."):
        local_setup_ownership.ensure_omlx(lambda stage: None, needs_repair=True, broken=broken)


def test_a_failed_ownership_read_on_repair_names_homebrew_not_a_manual_update(monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("installed by corsia local setup\n", encoding="utf-8")
    commands = []
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: marker)
    monkeypatch.setattr(local_setup_ownership, "run_command", commands.append)
    monkeypatch.setattr(local_setup_ownership.shutil, "which",
                        lambda name: "/opt/bin/brew" if name == "brew" else "/opt/bin/omlx")

    def locked(command):
        raise subprocess.CalledProcessError(1, command, stderr="Error: another brew process is running")

    monkeypatch.setattr(local_setup_ownership, "command_output", locked)

    with pytest.raises(RuntimeError,
                       match=r"Homebrew couldn't check oMLX: Error: another brew process is running\. Try again\."):
        local_setup_ownership.ensure_omlx(lambda stage: None, needs_repair=True)
    assert commands == []


def test_a_conflicting_tap_remote_reports_only_homebrews_last_line(monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("installed by corsia local setup\n", encoding="utf-8")
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: marker)
    monkeypatch.setattr(local_setup_ownership.shutil, "which",
                        lambda name: "/opt/bin/brew" if name == "brew" else "/opt/bin/omlx")
    monkeypatch.setattr(local_setup_ownership, "setup_owns_current_omlx", lambda *_args: True)

    def conflicting_remote(command):
        raise subprocess.CalledProcessError(1, command, stderr="old detail\ntap remote differs")

    monkeypatch.setattr(local_setup_ownership, "run_command", conflicting_remote)

    with pytest.raises(RuntimeError, match=r"Homebrew couldn't update oMLX: tap remote differs\. Try again\."):
        local_setup_ownership.ensure_omlx(lambda stage: None, needs_repair=True)


def test_an_empty_homebrew_stderr_has_no_raw_command_in_the_card_message(monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("installed by corsia local setup\n", encoding="utf-8")
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: marker)
    monkeypatch.setattr(local_setup_ownership.shutil, "which",
                        lambda name: "/opt/bin/brew" if name == "brew" else "/opt/bin/omlx")
    monkeypatch.setattr(local_setup_ownership, "setup_owns_current_omlx", lambda *_args: True,
                        raising=False)

    def failed(command):
        if command[1] == "tap":
            return None
        raise subprocess.CalledProcessError(1, command, stderr="")

    monkeypatch.setattr(local_setup_ownership, "run_command", failed)

    with pytest.raises(RuntimeError, match=r"Homebrew couldn't update oMLX\. Try again\."):
        local_setup_ownership.ensure_omlx(lambda stage: None, needs_repair=True, broken=False)
