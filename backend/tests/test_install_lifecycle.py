"""The lifecycle script honours the setup choice and mints no install API token
(onboarding 1a, #3286). Each case runs the real scripts; only effects
(drain, launchctl, process start) are stubbed and traced."""

from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.service_script_fixtures import run_bash

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"


def _bootstrap(tmp_path: Path, **keys) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 2, **keys}))
    return path


def _resolver_env(bootstrap: Path, **extra: str) -> dict[str, str]:
    return {
        "BOOTSTRAP_CONFIG": str(bootstrap),
        "BACKEND_DIR": str(BACKEND),
        "PYTHON": sys.executable,
        "CORSIA_ANALYTICS_DISABLE": "1",
        **extra,
    }


BAD = {"setup": "local", "bind_host": "0.0.0.0"}
LOCAL = {"setup": "local", "bind_host": "127.0.0.1"}


def test_shell_reader_matches_the_python_reader(tmp_path):
    script = "source scripts/corsia.d/runtime.sh\nresolve_bind_host"
    local = run_bash(script, env=_resolver_env(_bootstrap(tmp_path, **LOCAL)))
    assert (local.returncode, local.stdout) == (0, "127.0.0.1\n")
    bad = run_bash(script, env=_resolver_env(_bootstrap(tmp_path, **BAD)))
    assert bad.returncode != 0 and bad.stdout == ""
    assert "corsia setup local" in bad.stderr


def test_unmanaged_restart_refuses_a_bad_setup_before_draining(tmp_path):
    trace = tmp_path / "effects.log"
    result = run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
FORCE=false
source scripts/corsia.d/runtime.sh
require_port_served_by_this_checkout() { return 0; }
service_managed() { return 1; }
is_backend_running() { return 0; }
read_backend_start_iso() { printf 'start-iso'; }
drain_and_wait() { printf 'drain\\n' >> "$TRACE"; }
stop_backend() { printf 'stop\\n' >> "$TRACE"; }
start_backend() { printf 'start\\n' >> "$TRACE"; }
bounce_server
""",
        env=_resolver_env(_bootstrap(tmp_path, **BAD), TRACE=str(trace)),
    )
    assert result.returncode != 0
    assert not trace.exists()
    assert "corsia setup local" in result.stderr


def test_service_restart_refuses_a_bad_setup_before_any_launchd_effect(tmp_path):
    trace = tmp_path / "effects.log"
    agents = tmp_path / "agents"
    agents.mkdir()
    plist = agents / "com.corsia.prod.plist"
    plist.write_text("old plist")
    result = run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
BACKEND_LOG="$TRACE.backend.log"
CORSIA_WORKSPACE=/tmp/corsia/workspace
CORSIA_STATE_DIR=/tmp/corsia/state
ENV_FILE=/tmp/corsia-no-such-env
FORCE=false
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
service_definition_path() { printf '%s' "$PLIST"; }
service_label() { printf 'com.corsia.prod'; }
service_domain() { printf 'gui/501'; }
service_installed() { return 0; }
require_owned_service_definition() { return 0; }
service_is_loaded() { return 0; }
is_backend_running() { return 0; }
backend_pids() { printf '4242\\n'; }
require_loaded_service_owns_port() { return 0; }
launchctl() { printf 'launchctl %s\\n' "$*" >> "$TRACE"; }
drain_and_wait() { printf 'drain\\n' >> "$TRACE"; }
service_restart
""",
        env=_resolver_env(
            _bootstrap(tmp_path, **BAD), TRACE=str(trace), PLIST=str(plist)
        ),
    )
    assert result.returncode != 0
    assert not trace.exists()
    assert plist.read_text() == "old plist"
    assert sorted(p.name for p in agents.iterdir()) == [plist.name]


def test_start_backend_listens_on_the_chosen_interface(tmp_path):
    trace = tmp_path / "effects.log"
    result = run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
BACKEND_LOG="$TRACE.backend.log"
source scripts/corsia.d/runtime.sh
require_port_served_by_this_checkout() { return 0; }
is_backend_running() { return 1; }
backend_pids() { :; }
env() { printf '%s\\n' "$*" >> "$TRACE"; }
start_backend
wait
""",
        env=_resolver_env(_bootstrap(tmp_path, **LOCAL), TRACE=str(trace)),
    )
    assert result.returncode == 0, result.stderr
    assert "--host 127.0.0.1 --port 9999" in trace.read_text()


def test_start_backend_refuses_a_bad_setup_without_starting(tmp_path):
    trace = tmp_path / "effects.log"
    result = run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
source scripts/corsia.d/runtime.sh
require_port_served_by_this_checkout() { return 0; }
is_backend_running() { return 1; }
env() { printf '%s\\n' "$*" >> "$TRACE"; }
start_backend
""",
        env=_resolver_env(_bootstrap(tmp_path, **BAD), TRACE=str(trace)),
    )
    assert result.returncode != 0 and not trace.exists()


@pytest.mark.parametrize(
    ("keys", "host"),
    [(LOCAL, "127.0.0.1"), ({"setup": "server", "bind_host": "0.0.0.0"}, "0.0.0.0"), ({}, "0.0.0.0")],
)
def test_rendered_service_bakes_in_the_bind(tmp_path, keys, host):
    result = run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
BACKEND_LOG=/tmp/corsia/backend.log
CORSIA_WORKSPACE=/tmp/corsia/workspace
CORSIA_STATE_DIR=/tmp/corsia/state
ENV_FILE=/tmp/corsia-no-such-env
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
render_service_plist generation-test
""",
        env=_resolver_env(_bootstrap(tmp_path, **keys), HOME=str(tmp_path)),
    )
    assert result.returncode == 0, result.stderr
    arguments = plistlib.loads(result.stdout.encode())["ProgramArguments"]
    assert arguments[arguments.index("--host") + 1] == host


def test_print_endpoints_names_only_a_saved_outside_address(tmp_path):
    from infra.platform_paths import load_bootstrap, runtime_db_paths

    script = """
source scripts/corsia.d/runtime.sh
BACKEND_PORT=9999
tailscale() { echo 100.64.0.9; }
print_endpoints
"""
    home = tmp_path / "home"
    init = subprocess.run(
        [sys.executable, "-m", "cli", "init", "--workspace", str(tmp_path / "work"), "--setup", "server"],
        cwd=BACKEND,
        env={**{k: v for k, v in os.environ.items() if k not in {"CORSIA_WORKSPACE", "CORSIA_STATE_DIR"}},
             "HOME": str(home), "CORSIA_ANALYTICS_DISABLE": "1"},
        text=True, capture_output=True, check=False,
    )
    assert init.returncode == 0, init.stderr
    bootstrap = home / ".corsia" / "config.json"
    unsaved = run_bash(script, env=_resolver_env(bootstrap))
    assert unsaved.stdout == "  Local:     http://localhost:9999\n"
    db_path = runtime_db_paths(load_bootstrap(bootstrap).state_root)[0]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO settings VALUES ('devices', ?)",
                   (json.dumps({"enabled": True, "outside_address": "home.example.com"}),))
    saved = run_bash(script, env=_resolver_env(bootstrap))
    assert saved.stdout == "  Local:     http://localhost:9999\n  Outside address: home.example.com\n"

    # App mode reads the app's own state root, never this checkout install's saved address.
    app_mode = f"""
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/app_mode.sh
app_mode_install_overrides
_corsia_app_state_root={tmp_path / "app-state"}
BACKEND_PORT=9999
print_endpoints
"""
    app = run_bash(app_mode, env=_resolver_env(bootstrap))
    assert app.stdout == "  Local:     http://localhost:9999\n"


def test_health_probe_reaches_a_loopback_only_backend():
    """Every lifecycle probe targets 127.0.0.1, so a local setup stays manageable."""

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200 if self.path == "/api/health" else 404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = run_bash(
            "source scripts/corsia.d/runtime.sh\nBACKEND_PORT=$PORT\nis_backend_running",
            env={"PORT": str(server.server_port)},
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == 0, result.stderr


@pytest.fixture
def account_env(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".corsia").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "home").mkdir()
    env = {
        "HOME": str(tmp_path / "home"),
        "CORSIA_WORKSPACE": str(workspace),
        "CORSIA_STATE_DIR": str(state),
        "CORSIA_TEST_MODE": "1",
        "CORSIA_ANALYTICS_DISABLE": "1",
        "BACKEND_DIR": str(BACKEND),
        "PYTHON": sys.executable,
    }
    created = subprocess.run(
        [sys.executable, "-m", "cli", "user", "add", "owner", "--password-stdin"],
        cwd=BACKEND, env={**os.environ, **env}, input="correct horse battery\n",
        text=True, capture_output=True, check=False,
    )
    assert created.returncode == 0, created.stderr
    return env


def _kinds(env: dict) -> list[str]:
    listed = subprocess.run(
        [sys.executable, "-m", "cli", "token", "list"],
        cwd=BACKEND, env={**os.environ, **env}, text=True, capture_output=True, check=True,
    )
    return [line.split("\t")[1] for line in listed.stdout.splitlines() if "\t" in line]


def _run_sources_cli(env: dict):
    """Run run_sources_cli against a stub sources route; return (result, seen headers)."""
    seen: list[str] = []

    class Sources(BaseHTTPRequestHandler):
        def _answer(self):
            seen.append(self.headers.get("Authorization", ""))
            body = json.dumps({"sources": [
                {"source": "claude_code", "name": "Claude Code", "state": "ready"},
            ]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer  # noqa: N815

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Sources)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = run_bash(
            'source scripts/corsia.d/accounts.sh\nrun_sources_cli recheck',
            env={**env, "BACKEND_PORT": str(server.server_port)},
        )
    finally:
        server.shutdown()
        server.server_close()
    return result, seen


def test_source_summary_uses_a_service_token_and_leaves_no_api_token(account_env, tmp_path):
    result, seen = _run_sources_cli(account_env)
    assert result.returncode == 0, result.stderr
    assert "Claude Code" in result.stdout
    assert len(seen) == 1 and seen[0].startswith("Bearer ") and len(seen[0]) > len("Bearer ")
    secret = seen[0].removeprefix("Bearer ")
    assert secret not in result.stdout + result.stderr
    assert _kinds(account_env) == ["service"]
    assert not (tmp_path / "home" / ".corsia" / ".api-token").exists()


def test_scripts_no_longer_mint_an_install_api_token():
    accounts = (REPO / "scripts" / "corsia.d" / "accounts.sh").read_text()
    assert "ensure_install_token" not in accounts
    assert "--write-delegate-file" not in accounts
    assert "ensure_install_token" not in (REPO / "scripts" / "corsia").read_text()


def test_failed_mint_skips_the_summary_without_failing_start():
    result = run_bash(
        """
source scripts/corsia.d/accounts.sh
mint_service_token() { echo "no credential" >&2; return 1; }
run_sources_cli recheck
""",
        env={"BACKEND_DIR": str(BACKEND), "PYTHON": sys.executable},
    )
    assert result.returncode == 1
    assert "no credential" in result.stderr and result.stdout == ""
