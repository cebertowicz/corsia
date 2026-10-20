"""Broken entry points require ownership before setup repairs their installation."""

from __future__ import annotations

import subprocess

import pytest

from llm import local_setup_ownership as own

_REAL_PROBE = own._is_broken_leftover


@pytest.fixture
def leftover(tmp_path, monkeypatch):
    path = tmp_path / "omlx"
    path.write_text("#!/nonexistent\n", encoding="utf-8")
    monkeypatch.setattr(own, "_is_broken_leftover", _REAL_PROBE)
    monkeypatch.setattr(own.shutil, "which", lambda name: str(path) if name == "omlx" else "/opt/bin/brew")
    own._broken_cache.clear()
    return path


def _probe(monkeypatch, outcome):
    def run(command, **kwargs):
        assert kwargs["timeout"] == 15
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(own.subprocess, "run", run)


@pytest.mark.parametrize("outcome", [
    subprocess.CompletedProcess([], 126, "", "bad interpreter"),
    subprocess.CompletedProcess([], 127, "", ""),
    subprocess.CompletedProcess([], 1, "", "/x: bad interpreter: No such file"),
    OSError("exec format error"),
])
def test_a_leftover_that_cannot_run_reads_as_not_installed(leftover, monkeypatch, outcome):
    _probe(monkeypatch, outcome)
    assert own.installed_omlx() is None


def test_a_working_omlx_is_present(leftover, monkeypatch):
    _probe(monkeypatch, subprocess.CompletedProcess([], 0, "0.7.1", ""))
    assert own.installed_omlx() == str(leftover)



def test_a_slow_omlx_is_present_and_not_cached(leftover, monkeypatch):
    _probe(monkeypatch, subprocess.TimeoutExpired("omlx", 15))
    assert own.installed_omlx() == str(leftover)
    assert own._broken_cache == {}


def test_setup_reinstalls_only_a_proven_owned_broken_entry_point(leftover, monkeypatch):
    _probe(monkeypatch, subprocess.CompletedProcess([], 126, "", ""))
    commands: list[list[str]] = []

    def run(command):
        commands.append(command[1:])
        if command[1] == "install":
            raise subprocess.CalledProcessError(1, command, stderr="Could not symlink bin/omlx")

    monkeypatch.setattr(own, "run_command", run)
    from llm import local_setup_managed

    monkeypatch.setattr(local_setup_managed, "marker_path", lambda: leftover)
    monkeypatch.setattr(own, "command_output", lambda command: str(leftover.parent) if "--prefix" in command else "omlx 0.7.1")
    own.ensure_omlx(lambda stage: None, needs_repair=True, broken=True)

    assert commands[-1] == ["reinstall", own.OMLX_FORMULA]
    assert not any("--overwrite" in command or "install" in command for command in commands)


def test_a_real_install_failure_names_homebrew_and_the_reason(leftover, monkeypatch):
    _probe(monkeypatch, subprocess.CompletedProcess([], 126, "", ""))

    def run(command):
        if command[1] == "install":
            raise subprocess.CalledProcessError(1, command, stderr="Error: disk full\n")

    monkeypatch.setattr(own, "run_command", run)
    monkeypatch.setattr(own, "command_output", lambda command: "")
    monkeypatch.setattr(own.shutil, "which", lambda name: "/opt/bin/brew" if name == "brew" else None)

    with pytest.raises(RuntimeError, match=r"Homebrew couldn't install oMLX: Error: disk full\. Try again\."):
        own.ensure_omlx(lambda stage: None, needs_repair=True, broken=True)
