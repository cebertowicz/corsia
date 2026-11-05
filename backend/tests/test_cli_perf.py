"""Tests for historical performance CLI reporting."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

import cli_perf
from infra.request_timing import _BUCKET_UPPERS

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def api(monkeypatch):
    """Stub the shared corsia_api client and record its calls."""
    calls = []
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    monkeypatch.setattr(cli_perf.corsia_api, "resolve_base", lambda explicit: "http://api.test")
    monkeypatch.setattr(cli_perf.corsia_api, "resolve_token", lambda explicit: "tok")
    return calls


def _bucketed(duration_ms: float) -> list[int]:
    buckets = [0] * len(_BUCKET_UPPERS)
    buckets[next(i for i, upper in enumerate(_BUCKET_UPPERS) if duration_ms <= upper)] = 1
    return buckets


def test_routes_reads_through_shared_client_and_sorts_by_p95(api, monkeypatch, capsys):
    def request(method, base, path, token, body=None, query=None):
        api.append((method, base, path, token, query))
        return {"rows": [
            ["2026-09-25", "GET", "/api/fast", "2xx", 1, 3.0, _bucketed(3), 3_000, 3_000],
            ["2026-09-25", "GET", "/api/slow", "2xx", 1, 900.0, _bucketed(900), 900_000, 900_000],
            ["2026-09-25", "GET", "/other", "2xx", 1, 5_000.0, _bucketed(5_000), 5_000_000, 5_000_000],
        ]}

    monkeypatch.setattr(cli_perf.corsia_api, "request", request)

    assert cli_perf.cmd_routes(SimpleNamespace(days=3, route="/api/*")) == 0

    assert api == [("GET", "http://api.test", "/api/system/perf/routes", "tok", {"days": 3})]
    lines = capsys.readouterr().out.splitlines()
    start = (datetime.now(UTC).date() - timedelta(days=2)).isoformat()
    assert f"UTC days {start} through today" in lines[0]
    assert [line.split()[2] for line in lines[2:]] == ["/api/slow", "/api/fast"]


def test_routes_without_server_prints_one_line_and_exits_2(api, monkeypatch, capsys):
    def request(*args, **kwargs):
        raise SystemExit(1)

    monkeypatch.setattr(cli_perf.corsia_api, "request", request)

    assert cli_perf.cmd_routes(SimpleNamespace(days=7, route=None)) == 2
    assert capsys.readouterr().err == "The route timing report needs the running CORSIA server at http://api.test.\n"


def test_routes_disabled_does_not_call_server(api, monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    monkeypatch.setattr(cli_perf.corsia_api, "request", lambda *args, **kwargs: pytest.fail("called"))

    assert cli_perf.cmd_routes(SimpleNamespace(days=7, route=None)) == 2


def test_wrapper_perf_routes_authenticates_with_the_delegate_token_file(tmp_path):
    """`corsia perf routes` provisions the token file like the other API CLIs."""
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            seen.append({"path": url.path, **{k: v[0] for k, v in parse_qs(url.query).items()}})
            body = json.dumps({"rows": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token_file = tmp_path / ".api-token"
    token_file.write_text("file-token")
    token_file.chmod(0o600)
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "CORSIA_DIR": str(REPO_ROOT),
        "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
        "CORSIA_STATE_DIR": str(tmp_path / "state"),
        "CORSIA_API_TOKEN_FILE": str(token_file),
        "CORSIA_API_BASE": f"http://127.0.0.1:{server.server_port}",
        "CORSIA_TOKEN": "must-not-be-used",
    }
    try:
        proc = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "corsia"), "perf", "routes", "--days", "1"],
            capture_output=True, text=True, env=env, timeout=60,
        )
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert seen == [{"path": "/api/system/perf/routes", "days": "1", "token": "file-token"}]
    assert "No route timing data" in proc.stdout


def _tool_db(path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE messages (id TEXT, role TEXT, metadata TEXT, created_at TEXT);
        CREATE TABLE plan_tool_dispatches (started_at TEXT, settled_at TEXT);
        CREATE TABLE llm_usage (created_at TEXT, ttft_ms REAL);
    """)
    return db


def test_tools_reports_sqlite_aggregates_inside_the_window(tmp_path, monkeypatch, capsys):
    db = _tool_db(tmp_path / "corsia.db")
    # julianday keeps whole milliseconds, so sub-millisecond timestamps can round 250 ms to 251.
    now = datetime.now(UTC).replace(microsecond=0)
    old = (now - timedelta(days=5)).isoformat()
    db.executemany("INSERT INTO messages VALUES (?, ?, ?, ?)", [
        ("step", "assistant", "{}", now.isoformat()),
        ("tool", "tool", '{"assistant_step_id": "step"}', (now + timedelta(milliseconds=250)).isoformat()),
    ])
    db.executemany("INSERT INTO plan_tool_dispatches VALUES (?, ?)", [
        (now.isoformat(), (now + timedelta(seconds=1)).isoformat()),
        (old, old),
    ])
    db.executemany("INSERT INTO llm_usage VALUES (?, ?)", [(now.isoformat(), 40.0), (old, 9_999.0)])
    db.commit()
    monkeypatch.setattr("infra.database.get_db", lambda: db)

    assert cli_perf.cmd_tools(SimpleNamespace(days=2)) == 0

    rows = {line[:46].strip(): line[46:].split() for line in capsys.readouterr().out.splitlines()[2:]}
    assert rows["message tool result persistence (approximate)"] == ["1", "250.0", "250.0"]
    assert rows["plan tool dispatch"] == ["1", "1000.0", "1000.0"]
    assert rows["LLM time to first token"] == ["1", "40.0", "40.0"]


def test_tools_empty_window_is_not_zero_latency(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("infra.database.get_db", lambda: _tool_db(tmp_path / "corsia.db"))

    assert cli_perf.cmd_tools(SimpleNamespace(days=1)) == 0
    assert "this is not zero latency" in capsys.readouterr().out
