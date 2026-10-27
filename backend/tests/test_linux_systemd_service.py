"""Systemd user-service lifecycle with a stubbed user manager."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from service_script_fixtures import fake_checkout

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
  show)
    case "$5" in
      MainPID) echo 123 ;;
      FragmentPath) echo "${STUB_FRAGMENT:-$HOME/.config/systemd/user/com.corsia.prod.service}" ;;
      DropInPaths) echo "${STUB_DROPINS:-}" ;;
      NeedDaemonReload) echo "${STUB_STALE:-no}" ;;
    esac ;;
  daemon-reload|enable)
    if [ "${STUB_FAIL_ONCE:-}" = "$2" ] && [ ! -e "$STUB_FAILED" ]; then
      touch "$STUB_FAILED"
      exit 1
    fi ;;
esac
''')
    systemctl.chmod(0o755)
    return {**os.environ, "HOME": str(home), "PATH": f"{binary}:{os.environ['PATH']}",
            "BACKEND_DIR": str(backend), "BACKEND_PORT": "1956", "PYTHON": sys.executable,
            "ENV_FILE": str(tmp_path / "checkout" / ".env"), "CORSIA_WORKSPACE": str(workspace),
            "CORSIA_STATE_DIR": str(state), "BACKEND_LOG": str(state / "backend.log"),
            "SCRIPT_DIR": str(REPO / "scripts"), "STUB_CALLS": str(tmp_path / "calls"),
            "STUB_ACTIVE": str(tmp_path / "active"), "STUB_FAILED": str(tmp_path / "failed")}


def run_service(env, body):
    script = '''
set -e
fail() { echo "$*" >&2; }
warn() { echo "$*"; }
ok() { echo "$*"; }
resolve_bind_host() { echo 127.0.0.1; }
is_backend_running() { return 1; }
backend_pids() { :; }
backend_pid_serves_this_checkout() { [ "${STUB_FOREIGN_PID:-no}" = no ]; }
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
    assert calls(service_env).count("--user enable com.corsia.prod.service") >= 1
    assert "--user start com.corsia.prod.service" in calls(service_env)
    assert "--user stop com.corsia.prod.service" in calls(service_env)


def test_shared_directory_comparison_keeps_literal_and_physical_semantics(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    missing = tmp_path / "missing"
    script = '''source scripts/corsia.d/runtime.sh
same_directory "$1" "$1" && same_directory "$2" "$3" && ! same_directory "$1" "$2"
'''
    result = subprocess.run(
        ["/bin/bash", "-c", script, "test", str(missing), str(actual), str(alias)],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_stop_local_model_reports_linux_server_only(service_env):
    result = run_service(service_env, "service_install; service_start; STOP_LOCAL_MODEL=true; service_stop")
    assert result.returncode == 0, result.stderr
    assert "No local model runs on Linux; only the server is stopped." in result.stdout
    assert not Path(service_env["STUB_ACTIVE"]).exists()


def test_direct_stop_local_model_reports_linux_server_only(tmp_path):
    checkout = fake_checkout(tmp_path / "checkout")
    binary = tmp_path / "bin"
    binary.mkdir()
    for name, content in {
        "uname": "#!/bin/sh\necho Linux\n",
        "lsof": "#!/bin/sh\nexit 1\n",
        "curl": "#!/bin/sh\nexit 1\n",
        "systemctl": "#!/bin/sh\nexit 1\n",
    }.items():
        executable = binary / name
        executable.write_text(content)
        executable.chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path / "home"),
           "PATH": f"{binary}:{os.environ['PATH']}", "BACKEND_PORT": "45991",
           "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
           "CORSIA_STATE_DIR": str(tmp_path / "state"),
           "CORSIA_ENV_FILE": str(tmp_path / "missing.env")}
    result = subprocess.run(
        ["/bin/bash", str(checkout / "scripts/corsia"), "stop", "--stop-local-model"],
        env=env, capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "No local model runs on Linux; only the server is stopped." in result.stdout


def test_neutral_definition_path(service_env):
    result = run_service(service_env, "service_definition_path")
    assert result.returncode == 0
    assert result.stdout.strip().endswith("/com.corsia.prod.service")


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


@pytest.mark.parametrize("manager_override", [
    {"STUB_FRAGMENT": "/foreign/com.corsia.prod.service"},
    {"STUB_DROPINS": "/foreign/override.conf"},
    {"STUB_STALE": "yes"},
])
@pytest.mark.parametrize("force", ["true", "false"])
def test_stop_rejects_foreign_or_stale_manager_even_when_unhealthy(service_env, manager_override, force):
    assert run_service(service_env, "service_install; service_start").returncode == 0
    before = len(calls(service_env))
    result = run_service({**service_env, **manager_override}, f"FORCE={force}; service_stop")
    assert result.returncode != 0
    assert "--user stop com.corsia.prod.service" not in calls(service_env)[before:]
    assert Path(service_env["STUB_ACTIVE"]).exists()


def test_stop_rejects_a_foreign_running_process_even_when_forced(service_env):
    assert run_service(service_env, "service_install; service_start").returncode == 0
    result = run_service({**service_env, "STUB_FOREIGN_PID": "yes"}, "FORCE=true; service_stop")
    assert result.returncode != 0
    assert Path(service_env["STUB_ACTIVE"]).exists()


@pytest.mark.parametrize("failure", ["daemon-reload", "enable"])
def test_start_recovers_interrupted_install(service_env, failure):
    env = {**service_env, "STUB_FAIL_ONCE": failure}
    assert run_service(env, "service_install").returncode != 0
    result = run_service(env, "service_start")
    assert result.returncode == 0, result.stderr
    assert "--user daemon-reload" in calls(env)
    assert "--user enable com.corsia.prod.service" in calls(env)
    assert Path(env["STUB_ACTIVE"]).exists()


def test_restart_refreshes_old_owned_template_after_stopping(service_env):
    assert run_service(service_env, "service_install; service_start").returncode == 0
    unit = Path(service_env["HOME"]) / ".config/systemd/user/com.corsia.prod.service"
    result = run_service(service_env, '''
        eval "$(declare -f render_systemd_unit | sed '1s/render_systemd_unit/render_old_unit/')"
        render_systemd_unit() { render_old_unit | sed 's/RestartSec=5s/RestartSec=10s/'; }
        service_restart
    ''')
    assert result.returncode == 0, result.stderr
    assert "RestartSec=10s" in unit.read_text()
    assert Path(service_env["STUB_ACTIVE"]).exists()
