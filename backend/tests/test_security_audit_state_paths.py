"""Security-audit artifacts follow the selected CORSIA state root."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "security-audit.sh"


def _runtime_home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    install_root = home / ".corsia"
    python = install_root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(sys.executable))
    return home, install_root


def _audit_alert_path(
    *,
    home: Path,
    environment: dict[str, str],
) -> Path:
    isolated_environment = environment | {"HOME": str(home)}
    isolated_environment.pop("CI", None)
    isolated_environment.pop("GITHUB_ACTIONS", None)
    isolated_environment.pop("CORSIA_TEST_MODE", None)
    isolated_environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        ["/bin/bash", str(AUDIT_SCRIPT), "--print-alert-path"],
        check=True,
        capture_output=True,
        text=True,
        env=isolated_environment,
    )
    return Path(result.stdout.strip())


def test_audit_alert_path_uses_v2_bootstrap_state_root(tmp_path):
    home, install_root = _runtime_home(tmp_path)
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    (install_root / "config.json").write_text(
        json.dumps(
            {
                "version": 2,
                "workspace": {
                    "id": "workspace-123",
                    "name": "Test",
                    "root": str(workspace),
                },
                "state_root": str(state_root),
            }
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("CORSIA_WORKSPACE", None)
    environment.pop("CORSIA_STATE_DIR", None)

    assert _audit_alert_path(home=home, environment=environment) == (
        state_root / "security" / "ALERT"
    )


def test_audit_alert_path_honors_explicit_state_root(tmp_path):
    home, _install_root = _runtime_home(tmp_path)
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "explicit-state"
    environment = os.environ | {
        "CORSIA_WORKSPACE": str(workspace),
        "CORSIA_STATE_DIR": str(state_root),
    }

    assert _audit_alert_path(home=home, environment=environment) == (
        state_root / "security" / "ALERT"
    )


def test_audit_alert_path_preserves_legacy_bootstrap_state(tmp_path):
    home, install_root = _runtime_home(tmp_path)
    workspace = tmp_path / "legacy-workspace"
    (install_root / "config.json").write_text(
        json.dumps({"workspace": str(workspace)}),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("CORSIA_WORKSPACE", None)
    environment.pop("CORSIA_STATE_DIR", None)

    assert _audit_alert_path(home=home, environment=environment) == (
        workspace / ".corsia" / "security" / "ALERT"
    )


def test_security_alert_api_uses_selected_state_root(monkeypatch, tmp_path):
    from api import system

    state_root = tmp_path / "state"
    alert_path = state_root / "security" / "ALERT"
    alert_path.parent.mkdir(parents=True)
    alert_payload = {
        "vulns": 2,
        "errors": 0,
        "report": str(alert_path.with_name("audit.md")),
        "date": "2026-07-22",
    }
    alert_path.write_text(json.dumps(alert_payload), encoding="utf-8")
    monkeypatch.setattr(system.config, "STATE_ROOT", state_root)

    assert asyncio.run(system.security_alert()) == alert_payload


def test_security_alert_dismissal_uses_selected_state_root(
    monkeypatch,
    tmp_path,
):
    from api import system

    state_root = tmp_path / "state"
    alert_path = state_root / "security" / "ALERT"
    alert_path.parent.mkdir(parents=True)
    alert_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(system.config, "STATE_ROOT", state_root)
    monkeypatch.setattr(system, "authenticate", lambda token: {"id": token})

    assert asyncio.run(system.dismiss_security_alert("test-user")) == {"ok": True}
    assert not alert_path.exists()
