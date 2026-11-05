"""The person's ``gh`` sign-in probe on a gh older than ``auth status --json`` (v2.81.0).

The real probe runs; only the subprocess is scripted, so each case proves what
the connection row and the agent-facing GitHub state say for that gh.
"""

import json
import subprocess

import pytest

from connections import _gh_status as REAL_GH_STATUS

CUSTOM = "/opt/custom/gh"
JSON_STATUS = ("auth", "status", "--hostname", "github.com", "--active", "--json", "hosts")
PLAIN_STATUS = ("auth", "status", "--hostname", "github.com")
API_USER = ("api", "user", "--jq", ".login")
UNKNOWN_FLAG = b"unknown flag: --json\n\nUsage:  gh auth status [flags]\n"
NOT_LOGGED_IN = b"You are not logged into any GitHub hosts. To log in, run: gh auth login\n"


def _done(argv, code=0, out=b"", err=b""):
    return subprocess.CompletedProcess(argv, code, out, err)


@pytest.fixture
def gh(isolated_db, monkeypatch):
    """The real probe on a custom gh; ``gh.script`` maps an argv tail to (code, stdout, stderr)."""
    import os

    import connections
    from connections.models import ConnectionType, upsert_connection

    real_isfile, real_access = os.path.isfile, os.access
    monkeypatch.setattr(os.path, "isfile", lambda p: p == CUSTOM or real_isfile(p))
    monkeypatch.setattr(os, "access", lambda p, m: p == CUSTOM or real_access(p, m))
    monkeypatch.setattr(connections, "_gh_status", REAL_GH_STATUS)
    upsert_connection(type=ConnectionType.CLI, provider="github",
                      config={"binary": CUSTOM}, status="needs_auth")
    calls, script = [], {}

    def run(argv, **_kwargs):
        calls.append(tuple(argv[1:]))
        code, out, err = script[tuple(argv[1:])]
        return _done(argv, code, out, err)

    monkeypatch.setattr(subprocess, "run", run)
    gh.calls, gh.script = calls, script
    return gh


def _refresh():
    from connections import refresh_github_connection
    from connections.models import get_connection
    from tools.model_sources import github_state

    assert refresh_github_connection() is True
    return get_connection("github"), github_state()


def test_new_gh_signed_in_reads_the_json_report(gh):
    report = {"hosts": {"github.com": [{"active": True, "state": "success",
                                        "login": "person", "tokenSource": "keyring"}]}}
    gh.script[JSON_STATUS] = (0, json.dumps(report).encode(), b"")

    row, shown = _refresh()

    assert (row["status"], row["config"]["login"], shown) == ("available", "person", {"state": "ready"})
    assert gh.calls == [JSON_STATUS]


def test_old_gh_signed_in_gets_the_login_from_the_fallback(gh):
    gh.script[JSON_STATUS] = (1, b"", UNKNOWN_FLAG)
    gh.script[PLAIN_STATUS] = (0, b"github.com\n  Logged in to github.com account person\n", b"")
    gh.script[API_USER] = (0, b"person\n", b"")

    row, shown = _refresh()

    assert (row["status"], row["config"]["login"], shown) == ("available", "person", {"state": "ready"})
    assert gh.calls == [JSON_STATUS, PLAIN_STATUS, API_USER]  # api user: the ownership check's own call


def test_old_gh_without_a_login_asks_for_the_update_never_not_signed_in(gh):
    gh.script[JSON_STATUS] = (1, b"", UNKNOWN_FLAG)
    gh.script[PLAIN_STATUS] = (0, b"Logged in to github.com account person\n", b"")
    gh.script[API_USER] = (1, b"", b"gh: HTTP 502\n")

    row, shown = _refresh()

    assert row["status"] == "unverified"
    assert shown == {"state": "unverified", "reason": "Check failed: Update gh to 2.81.0 or later"}
    assert "login" not in row["config"]


@pytest.mark.parametrize("stderr", [
    NOT_LOGGED_IN,
    b'Hostname "github.com" not found among authenticated GitHub hosts\n',  # another host only
])
def test_old_gh_signed_out_needs_auth(gh, stderr):
    gh.script[JSON_STATUS] = (1, b"", UNKNOWN_FLAG)
    gh.script[PLAIN_STATUS] = (1, b"", stderr)

    row, shown = _refresh()

    assert (row["status"], shown) == ("needs_auth", {"state": "needs_auth"})
    assert gh.calls == [JSON_STATUS, PLAIN_STATUS]


def test_the_update_state_clears_once_gh_is_current(gh):
    from connections import refresh_github_connection

    gh.script[JSON_STATUS] = (1, b"", UNKNOWN_FLAG)
    gh.script[PLAIN_STATUS] = (0, b"", b"")
    gh.script[API_USER] = (1, b"", b"")
    refresh_github_connection()
    gh.script[JSON_STATUS] = (0, b'{"hosts":{}}', b"")

    row, shown = _refresh()

    assert (row["status"], shown) == ("needs_auth", {"state": "needs_auth"})
    assert "source_check" not in row["config"]


def test_a_non_flag_failure_is_still_an_unknown_probe_not_a_fallback(gh):
    gh.script[JSON_STATUS] = (1, b"", b"gh: keyring unavailable\n")

    from connections import refresh_github_connection

    assert refresh_github_connection(keep_row_on_failed_probe=True) is False
    assert gh.calls == [JSON_STATUS]
