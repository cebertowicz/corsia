"""Tests for the no-body provider CLI link route's filesystem helper."""

from __future__ import annotations

from pathlib import Path

import pytest

import cli_location
from api import provider_cli_link as link


def _binary(tmp_path: Path, name: str = "claude") -> Path:
    path = tmp_path / "login-bin" / name
    path.parent.mkdir()
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_link_ignores_shell_startup_junk_and_links(monkeypatch, tmp_path):
    source = _binary(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(link.Path, "home", lambda: home)
    monkeypatch.setattr(cli_location, "shell_binary", lambda _name: str(source))
    monkeypatch.setattr(cli_location, "service_path", lambda: str(home / ".local/bin"))
    monkeypatch.setattr(link, "service_path", cli_location.service_path)

    assert link.link_cli("claude-code") == "linked"
    assert (home / ".local/bin/claude").resolve() == source
    assert link.link_cli("claude-code") == "already_linked"


def test_link_reports_not_found_for_invalid_shell_path(monkeypatch, tmp_path):
    monkeypatch.setattr(link.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(cli_location, "shell_binary", lambda _name: "relative/claude")

    assert link.link_cli("claude-code") == "not_found"


def test_shell_binary_returns_none_when_the_configured_shell_cannot_start(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError("bad shell")

    monkeypatch.setattr(cli_location.subprocess, "Popen", unavailable)

    assert cli_location.shell_binary("claude") is None


def test_link_refuses_existing_different_file(monkeypatch, tmp_path):
    source = _binary(tmp_path)
    home = tmp_path / "home"
    destination = home / ".local/bin/claude"
    destination.parent.mkdir(parents=True)
    destination.write_text("other")
    monkeypatch.setattr(link.Path, "home", lambda: home)
    monkeypatch.setattr(cli_location, "shell_binary", lambda _name: str(source))

    assert link.link_cli("claude-code") == "conflict"
    assert destination.read_text() == "other"


def test_link_timeout_has_truthful_outcome(monkeypatch, tmp_path):
    monkeypatch.setattr(link.Path, "home", lambda: tmp_path / "home")

    def timeout(_name):
        raise TimeoutError

    monkeypatch.setattr(cli_location, "shell_binary", timeout)
    assert link.link_cli("claude-code") == "timed_out"


def test_unknown_vendor_is_not_a_valid_link_target():
    with pytest.raises(KeyError):
        link.link_cli("unknown")
