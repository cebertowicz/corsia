"""GitHub's one connection producer, run by startup and by the model-source recheck.

The suite stubs ``_gh_status`` for every test; these tests put the real probe
back and fake only the subprocess, so they prove the producer itself.
"""

import asyncio
import json
import subprocess
import threading

import pytest

from connections import _gh_status as REAL_GH_STATUS
from tests.test_provider_sources import _put, client  # noqa: F401

CUSTOM = "/opt/custom/gh"
STATUS = ("auth", "status", "--hostname", "github.com", "--active", "--json", "hosts")


def _report(answer: int) -> bytes:
    """``gh``'s JSON report: 0 is the person signed in as ``person``, 1 is signed out."""
    if answer == 1:
        return b'{"hosts":{}}'
    return json.dumps({"hosts": {"github.com": [{
        "active": True, "state": "success", "login": "person", "tokenSource": "keyring"}]}}).encode()


@pytest.fixture
def gh(isolated_db, monkeypatch):
    """The real probe on a configured custom ``gh``; ``gh.answer`` sets the outcome."""
    import os

    import connections

    real_isfile, real_access = os.path.isfile, os.access
    monkeypatch.setattr(os.path, "isfile", lambda p: p == CUSTOM or real_isfile(p))
    monkeypatch.setattr(os, "access", lambda p, m: p == CUSTOM or real_access(p, m))
    monkeypatch.setattr(connections, "_gh_status", REAL_GH_STATUS)
    _put("github", "available", binary=CUSTOM, keep="me")
    events: list = []
    answer: list = [0]

    real_run = subprocess.run

    def run(argv, **kwargs):
        if tuple(argv[1:]) != STATUS:
            return real_run(argv, **kwargs)
        events.append(("probe", tuple(argv), threading.current_thread().name))
        if isinstance(answer[0], BaseException):
            raise answer[0]
        return subprocess.CompletedProcess(argv, 0, _report(answer[0]), b"")

    monkeypatch.setattr(subprocess, "run", run)
    import connections.models as models

    real_upsert = models.upsert_connection

    def upsert(**kwargs):
        if kwargs["provider"] == "github":
            events.append(("write", "github"))
        return real_upsert(**kwargs)

    monkeypatch.setattr(models, "upsert_connection", upsert)
    gh.events, gh.answer = events, answer
    return gh


def _row():
    from connections.models import get_connection

    return get_connection("github")


@pytest.mark.parametrize(("answer", "status", "config"), [
    (0, "available", {"binary": CUSTOM, "keep": "me", "login": "person"}),
    (1, "needs_auth", {"binary": CUSTOM, "keep": "me"}),
])
def test_a_definite_answer_is_stored_with_the_resolved_binary_and_config(gh, answer, status, config):
    from connections import refresh_github_connection

    gh.answer[0] = answer
    assert refresh_github_connection(keep_row_on_failed_probe=True) is True
    assert gh.events == [("probe", (CUSTOM, *STATUS), threading.current_thread().name),
                         ("write", "github")]
    row = _row()
    assert row["status"] == status
    assert row["config"] == config


def test_a_signed_out_answer_keeps_the_last_known_login(gh):
    from connections import refresh_github_connection

    gh.answer[0] = 0
    refresh_github_connection()
    gh.answer[0] = 1
    refresh_github_connection()
    assert _row()["status"] == "needs_auth" and _row()["config"]["login"] == "person"


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("gh", 10), OSError("gone")])
def test_a_failed_probe_keeps_the_row_on_recheck_and_reads_needs_auth_at_startup(gh, failure):
    from connections import refresh_github_connection

    gh.answer[0] = failure
    before = _row()
    assert refresh_github_connection(keep_row_on_failed_probe=True) is False
    assert _row() == before and [e[0] for e in gh.events] == ["probe"]

    assert refresh_github_connection() is False  # startup keeps its classification
    assert _row()["status"] == "needs_auth"


def test_a_missing_binary_is_a_completed_not_installed_check(gh, monkeypatch):
    import connections

    monkeypatch.setattr(connections, "resolve_builtin_binary",
                        lambda provider, *, db=None: ({"keep": "me"}, None))
    assert connections.refresh_github_connection(keep_row_on_failed_probe=True) is True
    assert [e[0] for e in gh.events] == ["write"]
    assert _row()["status"] == "not_installed"


@pytest.fixture
def api(gh, client, monkeypatch):  # noqa: F811
    from api import provider_sources

    async def no_check(_provider, *, rediscover=False):
        return None

    monkeypatch.setattr(provider_sources, "_inflight", None)
    monkeypatch.setattr(provider_sources.network_discovery, "get_servers", lambda: [])
    monkeypatch.setattr(provider_sources, "check_harness_source", no_check)
    return provider_sources


@pytest.mark.asyncio
async def test_route_and_tool_share_one_refresh_probed_off_the_loop_before_the_write(api, gh):
    from tools.model_sources import ModelSourcesTool

    gh.answer[0] = 1
    tool = asyncio.create_task(ModelSourcesTool().execute(recheck=True))
    route = asyncio.create_task(api.recheck_sources({}))
    body, response = json.loads(await tool), await route

    assert len(gh.events) == 2, gh.events
    probe, write = gh.events
    assert probe[0] == "probe" and probe[2] != threading.current_thread().name
    assert write == ("write", "github")
    assert body["github"] == {"state": "needs_auth"}
    assert "github" not in {s["source"] for s in response["sources"]}


@pytest.mark.asyncio
async def test_a_failed_write_fails_the_recheck_with_the_safe_error(api, gh, monkeypatch):
    import connections.models as models
    from tools.model_sources import ModelSourcesTool

    def cannot_write(**_kwargs):
        raise OSError("database is locked")

    monkeypatch.setattr(models, "upsert_connection", cannot_write)
    with pytest.raises(api.SourceChecksIncomplete) as failed:
        await ModelSourcesTool().execute(recheck=True)
    assert "database" not in str(failed.value)


@pytest.mark.asyncio
async def test_after_a_failed_probe_the_fallback_read_is_the_stored_row(api, gh):
    from tools.model_sources import ModelSourcesTool

    gh.answer[0] = OSError("gone")
    with pytest.raises(api.SourceChecksIncomplete):
        await ModelSourcesTool().execute(recheck=True)
    body = json.loads(await ModelSourcesTool().execute())
    assert body["github"] == {"state": "ready"}  # last stored, no login, no retry
    assert [e[0] for e in gh.events] == ["probe"]


@pytest.mark.asyncio
async def test_a_failed_fallback_read_returns_no_rows(api, gh, monkeypatch):
    from tools.model_sources import ModelSourcesTool

    def unreadable():
        raise OSError("database is locked")

    monkeypatch.setattr(api, "list_sources", unreadable)
    with pytest.raises(OSError):
        await ModelSourcesTool().execute()
