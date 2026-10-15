"""A broken leftover `omlx` entry point reads as not installed; setup installs over it."""

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


def test_a_broken_leftover_never_requires_existing_omlx_consent(leftover, monkeypatch):
    _probe(monkeypatch, subprocess.CompletedProcess([], 127, "", ""))
    monkeypatch.setattr(own, "marker_path", lambda: leftover.parent / "owned")
    monkeypatch.setattr(own, "consent_marker_path", lambda: leftover.parent / "consented")

    assert own.existing_omlx_requires_consent() is False


def test_setup_owned_or_previously_consented_omlx_does_not_reprompt(monkeypatch, tmp_path):
    owned = tmp_path / "owned"
    consented = tmp_path / "consented"
    monkeypatch.setattr(own, "marker_path", lambda: owned)
    monkeypatch.setattr(own, "consent_marker_path", lambda: consented)
    monkeypatch.setattr(own.shutil, "which", lambda name: "/opt/bin/omlx" if name == "omlx" else None)
    monkeypatch.setattr(own, "_is_broken_leftover", lambda _path: False)

    assert own.existing_omlx_needs_consent_prompt() is True
    assert own.existing_omlx_requires_consent() is True
    owned.write_text("installed by corsia local setup\n", encoding="utf-8")
    assert own.existing_omlx_needs_consent_prompt() is False
    assert own.existing_omlx_requires_consent() is False
    owned.unlink()
    own.record_existing_omlx_consent()
    assert consented.exists()
    assert own.existing_omlx_needs_consent_prompt() is False
    assert own.existing_omlx_requires_consent() is False


def test_pre_auth_prompt_predicate_never_executes_omlx(monkeypatch, tmp_path):
    monkeypatch.setattr(own, "marker_path", lambda: tmp_path / "owned")
    monkeypatch.setattr(own, "consent_marker_path", lambda: tmp_path / "consented")
    monkeypatch.setattr(own.shutil, "which", lambda name: "/opt/bin/omlx" if name == "omlx" else None)
    monkeypatch.setattr(own, "_is_broken_leftover", lambda _path: pytest.fail("status must not execute oMLX"))

    assert own.existing_omlx_needs_consent_prompt() is True


def test_a_slow_omlx_is_present_and_not_cached(leftover, monkeypatch):
    _probe(monkeypatch, subprocess.TimeoutExpired("omlx", 15))
    assert own.installed_omlx() == str(leftover)
    assert own._broken_cache == {}


def test_setup_installs_over_a_leftover_and_links_it_over_the_stray_entry_point(leftover, monkeypatch):
    _probe(monkeypatch, subprocess.CompletedProcess([], 126, "", ""))
    commands: list[list[str]] = []

    def run(command):
        commands.append(command[1:])
        if command[1] == "install":
            raise subprocess.CalledProcessError(1, command, stderr="Could not symlink bin/omlx")

    monkeypatch.setattr(own, "run_command", run)
    monkeypatch.setattr(own, "command_output", lambda command: "omlx 0.7.1")
    monkeypatch.setattr(own, "mark_installed_by_setup", lambda: commands.append(["mark"]))

    own.ensure_omlx(lambda stage: None, needs_repair=True, broken=True)

    assert commands[-2:] == [["link", "--overwrite", "omlx"], ["mark"]]
    assert ["install", own.OMLX_FORMULA] in commands


def test_a_real_install_failure_names_homebrew_and_the_reason(leftover, monkeypatch):
    _probe(monkeypatch, subprocess.CompletedProcess([], 126, "", ""))

    def run(command):
        if command[1] == "install":
            raise subprocess.CalledProcessError(1, command, stderr="Error: disk full\n")

    monkeypatch.setattr(own, "run_command", run)
    monkeypatch.setattr(own, "command_output", lambda command: "")

    with pytest.raises(RuntimeError, match=r"Homebrew couldn't install oMLX: Error: disk full\. Try again\."):
        own.ensure_omlx(lambda stage: None, needs_repair=True, broken=True)
