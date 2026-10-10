"""``corsia sources`` formats the backend's model-source rows (onboarding 1a)."""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import cli_sources

SECRET = "tok-test-secret-value"


def _row(source: str, name: str, state: str, **extra) -> dict:
    return {
        "source": source,
        "name": name,
        "state": state,
        "prior_state": None,
        "reason": "",
        "fix": "",
        "checked_at": "2026-09-30T20:00:00+00:00",
        **extra,
    }


ROWS = [
    _row("claude-code", "Claude Code", "ready"),
    _row("codex", "Codex", "needs_auth", reason="Not signed in", fix="codex login"),
    _row("omlx", "oMLX", "needs_setup", reason="No model", fix="Open Settings"),
    _row("gemini", "Gemini", "not_installed", fix="npm i -g gemini"),
    _row("grok", "Grok", "unverified", reason="Check timed out"),
]


class _Stub:
    """A loopback HTTP server answering the sources routes."""

    def __init__(self, *, get_rows=None, recheck_rows=None, status=200, recheck_status=200):
        self.calls: list[tuple[str, str, str | None]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, code: int, rows) -> None:
                stub.calls.append(
                    (self.command, self.path, self.headers.get("Authorization"))
                )
                body = json.dumps({"sources": rows}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                self._answer(status, get_rows)

            def do_POST(self):  # noqa: N802
                self._answer(recheck_status, recheck_rows)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def run(monkeypatch, capsys):
    monkeypatch.delenv("CORSIA_SERVICE_PATH", raising=False)
    monkeypatch.setattr(cli_sources, "_read_token", lambda: SECRET)

    def _run(stub: _Stub, action: str | None = None) -> str:
        monkeypatch.setenv("CORSIA_API_BASE", stub.url)
        assert cli_sources.cmd_sources(argparse.Namespace(action=action)) == 0
        out = capsys.readouterr()
        assert SECRET not in out.out + out.err
        return out.out

    return _run


def test_each_source_gets_an_app_directed_summary_without_terminal_fixes(run):
    with _Stub(get_rows=ROWS) as stub:
        out = run(stub)
    assert stub.calls == [("GET", cli_sources.ROUTE, f"Bearer {SECRET}")]
    assert out.splitlines() == [
        "     Model sources",
        "       Claude Code: signed in",
        "       Codex: not signed in. Connect it in the app.",
        "       oMLX: not signed in. Connect it in the app.",
        "       Gemini: not signed in. Connect it in the app.",
        "       Grok: couldn't check. Check again in the app.",
    ]
    assert "→" not in out


def test_ready_source_with_a_verified_label_names_it(run):
    rows = [_row("codex", "Codex", "ready", accounts=[{
        "label": "steve@example.com", "label_verified": True,
    }])]
    with _Stub(get_rows=rows) as stub:
        out = run(stub)
    assert "Codex: signed in as steve@example.com" in out


def test_limited_source_keeps_its_signed_in_status(run, monkeypatch):
    monkeypatch.setattr(cli_sources, "_reset_time", lambda _value: "tomorrow")
    rows = [_row("codex", "Codex", "ready", limited=True, resets_at="later", accounts=[{
        "label": "steve@example.com", "label_verified": True,
    }])]
    with _Stub(get_rows=rows) as stub:
        out = run(stub)
    assert "Codex: signed in as steve@example.com · limited · back tomorrow" in out


def test_unverified_source_asks_for_an_app_recheck(run):
    rows = [_row("claude-code", "Claude Code", "unverified", prior_state="ready")]
    with _Stub(get_rows=rows) as stub:
        out = run(stub)
    assert "Claude Code: couldn't check. Check again in the app." in out
    assert cli_sources.NONE_READY not in out


def test_no_ready_source_adds_the_guidance_line(run):
    with _Stub(get_rows=ROWS[1:]) as stub:
        out = run(stub)
    assert out.rstrip().endswith(cli_sources.NONE_READY)


def test_backend_without_the_route_prints_one_line(run):
    with _Stub(get_rows=[], status=404) as stub:
        out = run(stub)
    assert out == f"{cli_sources.NOT_SUPPORTED}\n"
    assert cli_sources.NOT_SUPPORTED == "Model sources: this version can't list them here. See them in the app."


def test_recheck_posts_once(run):
    with _Stub(get_rows=[], recheck_rows=ROWS[:1]) as stub:
        out = run(stub, "recheck")
    assert [call[:2] for call in stub.calls] == [("POST", f"{cli_sources.ROUTE}/recheck")]
    assert "Claude Code: signed in" in out


def test_failed_recheck_names_the_stored_state_fallback(run):
    with _Stub(get_rows=ROWS[:1], recheck_status=500) as stub:
        out = run(stub, "recheck")
    assert [call[0] for call in stub.calls] == ["POST", "GET"]
    assert "Model sources: recheck failed: HTTP Error 500:" in out
    assert "showing last stored state." in out
    assert "Claude Code: signed in" in out


def test_unreachable_backend_never_fails_the_caller(run, monkeypatch):
    stub = _Stub()
    stub.server.server_close()  # nothing listens on this port any more
    out = run(stub)
    assert out.startswith("Model sources: couldn't check · ")
    assert out.rstrip().endswith(". See them in the app.")
    assert "corsia sources" not in out


def test_missing_token_points_at_the_fix(monkeypatch, capsys):
    monkeypatch.delenv(cli_sources.TOKEN_ENV, raising=False)
    assert cli_sources.cmd_sources(argparse.Namespace(action=None)) == 0
    assert capsys.readouterr().out == "Model sources: couldn't check from here. See them in the app.\n"


def test_token_comes_from_the_environment_only(monkeypatch):
    monkeypatch.setenv(cli_sources.TOKEN_ENV, f"  {SECRET}  ")
    assert cli_sources._read_token() == SECRET
    monkeypatch.setenv(cli_sources.TOKEN_ENV, "  ")
    assert cli_sources._read_token() is None


def test_invisible_binary_is_never_printed_as_a_terminal_fix(run):
    rows = [_row("codex", "Codex", "not_installed", invisible_binary="/private/bin/codex")]
    with _Stub(get_rows=rows) as stub:
        out = run(stub)
    assert "Codex: not signed in. Connect it in the app." in out
    assert "/private/bin/codex" not in out
