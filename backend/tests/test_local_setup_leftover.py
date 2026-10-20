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
    assert own._is_broken_leftover(str(leftover)) is True


def test_a_working_omlx_is_present(leftover, monkeypatch):
    _probe(monkeypatch, subprocess.CompletedProcess([], 0, "0.7.1", ""))
    assert own._is_broken_leftover(str(leftover)) is False



def test_a_slow_omlx_is_present_and_not_cached(leftover, monkeypatch):
    _probe(monkeypatch, subprocess.TimeoutExpired("omlx", 15))
    assert own._is_broken_leftover(str(leftover)) is False
    assert own._broken_cache == {}


def test_setup_refuses_to_repair_a_marked_broken_entry_point(leftover, monkeypatch):
    _probe(monkeypatch, subprocess.CompletedProcess([], 126, "", ""))
    commands: list[list[str]] = []

    def run(command):
        commands.append(command[1:])
        if command[1] == "install":
            raise subprocess.CalledProcessError(1, command, stderr="Could not symlink bin/omlx")

    monkeypatch.setattr(own, "run_command", run)
    from llm import managed_omlx_binary

    monkeypatch.setattr(managed_omlx_binary, "marker_path", lambda: leftover)
    monkeypatch.setattr(own.managed, "command_output", lambda command: str(leftover.parent) if "--prefix" in command else "omlx 0.7.1")
    with pytest.raises(own.OwnOmlxUpgradeRequired, match="brew reinstall jundot/omlx/omlx"):
        own.ensure_omlx(lambda stage: None, needs_repair=True, broken=True)
    assert commands == []


def test_source_setup_leaves_an_unowned_broken_leftover_untouched(leftover, monkeypatch, tmp_path):
    _probe(monkeypatch, subprocess.CompletedProcess([], 126, "", "bad interpreter"))
    commands = []
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(own, "run_command", commands.append)
    with pytest.raises(own.OwnOmlxUpgradeRequired, match="brew reinstall jundot/omlx/omlx"):
        own.ensure_omlx(lambda _stage: None, needs_repair=True, broken=True)
    assert commands == []
    assert not own.managed.marker_path().exists()


def test_packaged_setup_ignores_a_broken_path_leftover_without_brew(leftover, monkeypatch):
    monkeypatch.setattr(own.managed, "managed_omlx_executable", lambda: "/Applications/CORSIA.app/Contents/Resources/bin/omlx")
    monkeypatch.setattr(own, "omlx_on_path", lambda: pytest.fail("bundle must not probe PATH"))
    monkeypatch.setattr(own, "run_command", lambda _command: pytest.fail("bundle must not call brew"))
    own.ensure_omlx(lambda _stage: None, needs_repair=True, broken=True)


def test_a_real_install_failure_names_homebrew_and_the_reason(leftover, monkeypatch):
    _probe(monkeypatch, subprocess.CompletedProcess([], 126, "", ""))

    def run(command):
        if command[1] == "install":
            raise subprocess.CalledProcessError(1, command, stderr="Error: disk full\n")

    monkeypatch.setattr(own, "run_command", run)
    monkeypatch.setattr(own.managed, "command_output", lambda command: "")
    monkeypatch.setattr(own.shutil, "which", lambda name: "/opt/bin/brew" if name == "brew" else None)

    with pytest.raises(RuntimeError, match=r"Homebrew couldn't install oMLX: Error: disk full\. Try again\."):
        own.ensure_omlx(lambda stage: None, needs_repair=True, broken=True)
