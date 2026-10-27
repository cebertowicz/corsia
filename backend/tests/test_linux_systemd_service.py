"""Systemd user-service lifecycle with a stubbed user manager."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SERVICE = REPO / "scripts" / "corsia.d" / "systemd.sh"


@pytest.fixture
def service_env(tmp_path):
    home = tmp_path / "home with spaces"
    home.mkdir()
    backend = tmp_path / "checkout" / "backend"
    backend.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    workspace = tmp_path / "work"
    workspace.mkdir()
    binary = tmp_path / "bin"
    binary.mkdir()
    systemctl = binary / "systemctl"
    systemctl.write_text('''#!/bin/bash
set -eu
echo "$*" >> "$STUB_CALLS"
case "$2" in
  is-active) [ -e "$STUB_ACTIVE" ] ;;
  start) touch "$STUB_ACTIVE" ;;
  stop) rm -f "$STUB_ACTIVE" ;;
  show) echo 123 ;;
esac
''')
    systemctl.chmod(0o755)
    return {**os.environ, "HOME": str(home), "PATH": f"{binary}:{os.environ['PATH']}",
            "BACKEND_DIR": str(backend), "BACKEND_PORT": "1956", "PYTHON": sys.executable,
            "ENV_FILE": str(tmp_path / "checkout" / ".env"), "CORSIA_WORKSPACE": str(workspace),
            "CORSIA_STATE_DIR": str(state), "BACKEND_LOG": str(state / "backend.log"),
            "SCRIPT_DIR": str(REPO / "scripts"), "STUB_CALLS": str(tmp_path / "calls"),
            "STUB_ACTIVE": str(tmp_path / "active")}


def run_service(env, body):
    script = '''
set -e
fail() { echo "$*" >&2; }
warn() { echo "$*"; }
ok() { echo "$*"; }
resolve_bind_host() { echo 127.0.0.1; }
is_backend_running() { return 1; }
backend_pids() { :; }
source "$SERVICE_SCRIPT"
FORCE=false
''' + body
    return subprocess.run(["/bin/bash", "-c", script], env={**env, "SERVICE_SCRIPT": str(SERVICE)},
                          capture_output=True, text=True, check=False)


def calls(env):
    path = Path(env["STUB_CALLS"])
    return path.read_text().splitlines() if path.exists() else []


def test_install_start_stop_and_idempotent_install(service_env):
    result = run_service(service_env, "service_install; service_start; service_install; service_stop")
    assert result.returncode == 0, result.stderr
    unit = Path(service_env["HOME"]) / ".config/systemd/user/com.corsia.prod.service"
    content = unit.read_text()
    assert "WantedBy=default.target" in content
    assert "Restart=on-failure" in content and "RestartSec=5s" in content
    assert f"WorkingDirectory={service_env['BACKEND_DIR']}\n" in content
    assert f"StandardOutput=append:{service_env['BACKEND_LOG']}\n" in content
    assert "NoNewPrivileges" not in content and "ProtectHome" not in content
    assert calls(service_env).count("--user enable com.corsia.prod.service") == 1
    assert "--user start com.corsia.prod.service" in calls(service_env)
    assert "--user stop com.corsia.prod.service" in calls(service_env)


def test_restart_activates_release_between_stop_and_start(service_env):
    result = run_service(service_env, 'service_install; service_start; activate() { echo activated >> "$STUB_CALLS"; }; service_restart activate')
    assert result.returncode == 0, result.stderr
    events = calls(service_env)
    assert events.index("--user stop com.corsia.prod.service") < events.index("activated")
    assert events.index("activated") < len(events) - 1
    assert events[-1] == "--user start com.corsia.prod.service"


def test_foreign_unit_is_never_overwritten(service_env):
    unit = Path(service_env["HOME"]) / ".config/systemd/user/com.corsia.prod.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("foreign definition\n")
    result = run_service(service_env, "service_install")
    assert result.returncode != 0
    assert unit.read_text() == "foreign definition\n"
    assert not calls(service_env)
