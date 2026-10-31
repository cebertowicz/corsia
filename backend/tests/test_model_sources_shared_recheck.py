"""The agent tool and the HTTP route share one source recheck and one failure."""

import asyncio
import json

import pytest

from tests.test_provider_sources import _put, client  # noqa: F401

SAFE = "Model source checks could not be completed. Check again."


async def _fine(*_args, **_kwargs):
    return None


async def _started(entered: asyncio.Event) -> None:
    """Fail, rather than wait forever, when a caller never reaches the checks."""
    await asyncio.wait_for(entered.wait(), timeout=5)


@pytest.fixture
def api(client, monkeypatch):  # noqa: F811
    """The real source owner with no host sign-in and no servers to sweep."""
    from api import provider_sources

    monkeypatch.setattr(provider_sources, "_inflight", None)
    monkeypatch.setattr(provider_sources.network_discovery, "get_servers", lambda: [])
    return provider_sources


def test_schema_has_one_optional_boolean():
    from tools.model_sources import ModelSourcesTool

    schema = ModelSourcesTool().parameters
    assert schema["properties"] == {"recheck": {
        "type": "boolean",
        "description": "Re-run every source check once before reading.",
    }}
    assert "required" not in schema
    assert schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_plain_read_imports_the_real_owner_and_never_probes(api, monkeypatch):
    from tools.model_sources import ModelSourcesTool

    started = []

    async def recheck():
        started.append(True)

    monkeypatch.setattr(api, "_recheck", recheck)
    _put("github", "available", login="octo")
    body = json.loads(await ModelSourcesTool().execute())

    assert set(body) == {"sources", "github", "github_agent"}
    assert body["sources"] == api.list_sources()
    assert body["github"] == {"state": "ready"}  # the person's stored login is never shown
    assert body["github_agent"] == {"state": "not_installed"}
    assert "github" not in {s["source"] for s in body["sources"]}
    assert started == [] and api._inflight is None


@pytest.mark.asyncio
async def test_tool_and_route_share_one_run_and_one_result(api, monkeypatch):
    from tools.model_sources import ModelSourcesTool

    entered, release, runs = asyncio.Event(), asyncio.Event(), []

    async def check(provider, *, rediscover):
        runs.append(provider)
        entered.set()
        await release.wait()

    monkeypatch.setattr(api, "check_harness_source", check)
    tool = asyncio.create_task(ModelSourcesTool().execute(recheck=True))
    await _started(entered)
    route = asyncio.create_task(api.recheck_sources({}))
    await asyncio.sleep(0)
    release.set()
    body, response = json.loads(await tool), await route

    assert sorted(runs) == ["claude-code", "codex"]
    assert set(response) == {"sources"} and set(body) == {"sources", "github", "github_agent"}
    assert body["sources"] == response["sources"]


@pytest.mark.asyncio
async def test_incomplete_recheck_fails_both_callers_alike_then_recovers(api, monkeypatch):
    from fastapi import HTTPException

    from connections.source_state import apply_check
    from tools.model_sources import ModelSourcesTool

    _put("claude-code", "needs_auth")
    entered, release, finished = asyncio.Event(), asyncio.Event(), []

    async def check(provider, *, rediscover):
        entered.set()
        await release.wait()
        finished.append(provider)
        if provider == "claude-code":
            apply_check(provider, "ready")
        if provider == "codex":
            raise OSError("private diagnostic must not enter response")

    monkeypatch.setattr(api, "check_harness_source", check)
    tool = asyncio.create_task(ModelSourcesTool().execute(recheck=True))
    await _started(entered)
    route = asyncio.create_task(api.recheck_sources({}))
    await asyncio.sleep(0)
    release.set()
    tool_result, route_result = await asyncio.gather(tool, route, return_exceptions=True)

    assert sorted(finished) == ["claude-code", "codex"]  # one run, every child settled
    assert isinstance(tool_result, api.SourceChecksIncomplete)
    assert isinstance(route_result, HTTPException) and route_result.status_code == 503
    assert str(tool_result) == route_result.detail == SAFE
    assert tool_result.__cause__ is None and tool_result.__suppress_context__
    assert api._inflight.done()
    assert api.list_sources()[0]["state"] == "ready"  # legitimate partial write survives

    monkeypatch.setattr(api, "check_harness_source", _fine)
    body = json.loads(await ModelSourcesTool().execute(recheck=True))
    assert body["sources"] == (await api.recheck_sources({}))["sources"]


def _gh(monkeypatch, probe):
    """A resolved ``gh`` off PATH whose sign-in probe is ``probe(binary)``."""
    import connections

    monkeypatch.setattr(connections, "resolve_builtin_binary",
                        lambda provider, *, db=None: ({}, f"/opt/test/{provider}"))
    monkeypatch.setattr(connections, "_gh_status", lambda binary: (probe(binary), None))


@pytest.mark.asyncio
async def test_recheck_refreshes_github_once_off_the_loop_for_concurrent_callers(api, monkeypatch):
    import threading

    from tools.model_sources import ModelSourcesTool

    _put("github", "needs_auth")
    loop_thread, probes = threading.get_ident(), []

    def signed_in(binary):
        probes.append((binary, threading.get_ident() != loop_thread))
        return True

    entered, release = asyncio.Event(), asyncio.Event()

    async def check(_provider, *, rediscover):
        entered.set()
        await release.wait()

    _gh(monkeypatch, signed_in)
    monkeypatch.setattr(api, "check_harness_source", check)
    tool = asyncio.create_task(ModelSourcesTool().execute(recheck=True))
    await _started(entered)
    route = asyncio.create_task(api.recheck_sources({}))
    await asyncio.sleep(0)
    release.set()
    body, _response = json.loads(await tool), await route

    assert probes == [("/opt/test/github", True)]
    assert body["github"] == {"state": "ready"}


@pytest.mark.asyncio
async def test_a_failed_github_probe_fails_the_recheck_and_keeps_the_last_row(api, monkeypatch):
    from connections.models import get_connection
    from tools.model_sources import ModelSourcesTool

    _put("github", "available")
    before = get_connection("github")
    _gh(monkeypatch, lambda _binary: None)
    monkeypatch.setattr(api, "check_harness_source", _fine)

    with pytest.raises(api.SourceChecksIncomplete):
        await ModelSourcesTool().execute(recheck=True)
    assert get_connection("github") == before
    # The one plain read afterwards shows the stored row, not an invented one.
    assert json.loads(await ModelSourcesTool().execute())["github"] == {"state": "ready"}


def test_startup_still_stores_a_failed_github_probe_as_needs_auth(api, monkeypatch):
    import connections
    from connections.models import get_connection

    probed = []
    _gh(monkeypatch, lambda binary: probed.append(binary))
    connections.seed_builtin_connections()

    assert probed == ["/opt/test/github"]
    assert get_connection("github")["status"] == "needs_auth"


@pytest.mark.asyncio
async def test_a_cancelled_caller_leaves_the_shared_run_to_finish(api, monkeypatch):
    entered, release, finished = asyncio.Event(), asyncio.Event(), []

    async def check(provider, *, rediscover):
        entered.set()
        await release.wait()
        finished.append(provider)

    monkeypatch.setattr(api, "check_harness_source", check)
    first = asyncio.create_task(api.recheck_sources_once())
    await _started(entered)
    second = asyncio.create_task(api.recheck_sources_once())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not api._inflight.done()
    release.set()

    assert await second == api.list_sources()
    assert sorted(finished) == ["claude-code", "codex"]
