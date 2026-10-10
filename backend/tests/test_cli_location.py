"""The one place that answers where a vendor CLI lives and whether the service sees it."""

from __future__ import annotations

from pathlib import Path

import pytest

import cli_location


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_service_path_defaults_to_the_launchd_services_path(monkeypatch, tmp_path):
    monkeypatch.delenv("CORSIA_SERVICE_PATH", raising=False)
    monkeypatch.setattr(cli_location.Path, "home", lambda: tmp_path)
    assert cli_location.service_path() == ":".join((
        str(tmp_path / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ))


def test_service_path_default_matches_the_shell_services_source(monkeypatch):
    """The shell script is the source of the default; this module must match it."""
    monkeypatch.delenv("CORSIA_SERVICE_PATH", raising=False)
    script = (Path(__file__).resolve().parents[2] / "scripts/corsia.d/service.sh").read_text()
    line = next(row for row in script.splitlines() if row.startswith("SERVICE_PATH="))
    shell_default = line.split("=", 1)[1].strip('"').replace("$HOME", str(Path.home()))
    assert cli_location.service_path() == shell_default


def test_service_path_honours_the_override(monkeypatch):
    monkeypatch.setenv("CORSIA_SERVICE_PATH", "/custom/bin")
    assert cli_location.service_path() == "/custom/bin"


def test_service_binary_sees_only_the_service_path(monkeypatch, tmp_path):
    visible = _executable(tmp_path / "service-bin" / "codex")
    _executable(tmp_path / "login-bin" / "claude")
    monkeypatch.setenv("CORSIA_SERVICE_PATH", str(visible.parent))
    assert cli_location.service_binary("codex") == str(visible)
    assert cli_location.service_binary("claude-code") is None
    assert cli_location.service_binary("unknown") is None


def test_every_supported_vendor_has_a_binary():
    assert cli_location.BINARIES == {"claude-code": "claude", "codex": "codex", "omlx": "omlx"}


def test_validated_binary_rejects_untrusted_candidates(tmp_path):
    home = tmp_path / "home"
    good = _executable(tmp_path / "bin" / "claude")
    assert cli_location.validated_binary("claude", str(good), home) == good.resolve()
    assert cli_location.validated_binary("claude", None, home) is None
    assert cli_location.validated_binary("claude", "relative/claude", home) is None
    assert cli_location.validated_binary("claude", str(_executable(tmp_path / "bin" / "other")), home) is None
    assert cli_location.validated_binary("claude", str(_executable(home / ".local/bin" / "claude")), home) is None


def test_login_shell_binary_is_none_for_an_unknown_vendor():
    assert cli_location.login_shell_binary("unknown") is None


def test_login_shell_binary_lets_a_hung_shell_raise(monkeypatch):
    def hang(_binary):
        raise TimeoutError

    monkeypatch.setattr(cli_location, "shell_binary", hang)
    with pytest.raises(TimeoutError):
        cli_location.login_shell_binary("codex")
