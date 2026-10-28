"""A recheck cannot present prior facts as proof an incomplete check succeeded."""

import asyncio
import importlib

import pytest

from tests.test_provider_sources import (  # noqa: F401
    _listed, _model, _put, _server, _sources, claude_session, client,
)


pytestmark = pytest.mark.usefixtures("mac_local_model_host")


@pytest.mark.real_harness_cli
@pytest.mark.parametrize("answer", [None, OSError("database is locked")])
def test_primary_and_error_path_writes_failing_never_return_prior_row_as_fresh(
    isolated_db, client, claude_session, monkeypatch, answer,
):
    from connections import source_state

    _put("claude-code", "needs_auth")
    claude_session(_listed())
    calls = []

    def failed(*args, **_kwargs):
        calls.append(args)
        if isinstance(answer, Exception):
            raise answer
        return None

    async def check(provider, *, rediscover):
        if provider == "claude-code":
            await source_state.check_harness_source(provider, rediscover=rediscover)

    monkeypatch.setattr(source_state, "record_source_check", failed)
    monkeypatch.setattr("api.provider_sources.check_harness_source", check)
    response = client.post("/api/providers/sources/recheck")
    assert response.status_code == 503
    assert "sources" not in response.json()
    assert "database" not in response.text
    assert len(calls) == 2  # READY and the explicit UNVERIFIED failure receipt
    assert _sources(client)["claude-code"]["state"] == "needs_auth"


@pytest.mark.parametrize("answer", [None, OSError("database is locked")])
def test_all_check_writes_failing_returns_safe_error(isolated_db, client, monkeypatch, answer):
    import connections
    from connections import source_state

    _put("claude-code", "needs_auth")
    _put("codex", "available")
    calls = []

    def failed(*args, **_kwargs):
        calls.append(args)
        if isinstance(answer, Exception):
            raise answer
        return None

    monkeypatch.setattr(connections, "resolve_builtin_binary", lambda provider, *, db=None: ({}, None))
    monkeypatch.setattr("api.provider_sources.check_harness_source", source_state.check_harness_source)
    monkeypatch.setattr(source_state, "record_source_check", failed)
    response = client.post("/api/providers/sources/recheck")
    assert response.status_code == 503
    assert response.json() == {"detail": "Model source checks could not be completed. Check again."}
    assert len(calls) == 2
    assert _sources(client)["claude-code"]["state"] == "needs_auth"


def test_network_failure_does_not_create_a_get_overlay(isolated_db, client, monkeypatch):
    from llm import local_sources, network_discovery as nd

    sid = _server(provider="external", kind="external", name="Lab")
    _model(f"external@{sid}/m")

    async def failure(*_args):
        return {"ok": False, "message": "transport failed"}

    monkeypatch.setattr("api.external_connection_check.check_external_server", failure)
    assert client.post("/api/providers/sources/recheck").status_code == 503
    nd.update_server_status(sid, status="offline")
    assert _sources(client)[f"external:{sid}"]["state"] == "unverified"
    nd.update_server_status(sid, status="online")  # health/Test's existing writer
    before = _sources(client)[f"external:{sid}"]
    assert before["state"] == "ready"
    importlib.reload(local_sources)
    assert _sources(client)[f"external:{sid}"] == before


@pytest.mark.asyncio
async def test_single_flight_settles_failures_and_can_recover(isolated_db, client, monkeypatch):
    from fastapi import HTTPException
    from api import provider_sources as api
    from connections.source_state import apply_check

    _put("claude-code", "needs_auth")
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = []

    async def check(provider, *, rediscover):
        entered.set()
        await release.wait()
        finished.append(provider)
        if provider == "claude-code":
            apply_check(provider, "ready")
        if provider == "codex":
            raise OSError("private diagnostic must not enter response")

    monkeypatch.setattr(api, "check_harness_source", check)
    monkeypatch.setattr(api, "_inflight", None)
    monkeypatch.setattr(api.network_discovery, "get_servers", lambda: [])
    first = asyncio.create_task(api.recheck_sources({}))
    await entered.wait()
    second = asyncio.create_task(api.recheck_sources({}))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert sorted(finished) == ["claude-code", "codex"]
    assert all(isinstance(r, HTTPException) and r.status_code == 503 for r in results)
    assert results[0].detail == results[1].detail
    assert api._inflight.done()
    assert api.list_sources()[0]["state"] == "ready"  # legitimate partial write survives

    async def fine(*_args, **_kwargs):
        return None

    monkeypatch.setattr(api, "check_harness_source", fine)
    assert set(await api.recheck_sources({})) == {"sources"}


@pytest.mark.parametrize("endpoint_count", [1, 8])
def test_catalog_reads_are_constant_in_endpoint_count(isolated_db, client, endpoint_count):
    from api.provider_sources import list_sources
    from infra.database import get_db

    managed = _server()
    _model(f"omlx@{managed}/m", enabled=0)  # oMLX does not require enabled
    for i in range(endpoint_count):
        sid = _server(provider="external", kind="external", name=f"Lab {i}")
        _model(f"external@{sid}/m")
    statements = []
    db = get_db()
    db.set_trace_callback(statements.append)
    try:
        sources = list_sources()
    finally:
        db.set_trace_callback(None)
    model_reads = [s for s in statements if s.lstrip().upper().startswith("SELECT")
                   and "FROM models" in s]
    # One read per local/external source kind, and one per harness vendor for its account lines.
    assert len(model_reads) == 4
    assert all(s["state"] == "ready" for s in sources[2:])


@pytest.mark.parametrize("selector,enabled,available,ready", [
    ("external@{sid}/m", 1, 1, True),
    ("external@{sid}/m", 0, 1, False),
    ("external@{sid}/m", 1, 0, False),
    ("external@{sid}-other/m", 1, 1, False),
    ("external@{sid}", 1, 1, False),
    ("omlx@{sid}/m", 1, 1, False),
])
def test_membership_preserves_exact_identity_and_external_eligibility(
    isolated_db, client, selector, enabled, available, ready,
):
    sid = _server(provider="external", kind="external", name="Lab")
    _model(selector.format(sid=sid), enabled=enabled, available=available)
    source = _sources(client)[f"external:{sid}"]
    assert (source["state"] == "ready") is ready
    assert source["fix"] == (None if ready else "Check again")


@pytest.mark.parametrize("recovery", ["health", "test"])
def test_real_health_and_test_recovery_are_readable_without_overlay(
    isolated_db, client, monkeypatch, recovery,
):
    import httpx
    from api import external_connection_check as check
    from llm import local_sources, network_discovery as nd
    from omlx_provider_fixtures import _patch_httpx, _resp, _Router

    sid = _server(provider="external", kind="external", name="Lab",
                  base_url="http://127.0.0.1:8000/v1")
    _model(f"external@{sid}/m")
    original = check.check_external_server

    async def failed(*_args):
        return {"ok": False, "message": "transport failed"}

    monkeypatch.setattr(check, "check_external_server", failed)
    # Seed the failed attempt; the assertion is recovery through the real writer.
    # Failure HTTP behavior is covered separately, so the reverted tree reaches
    # the recovery assertion and exposes its persistent process overlay.
    client.post("/api/providers/sources/recheck")
    monkeypatch.setattr(check, "check_external_server", original)
    _patch_httpx(monkeypatch, _Router({
        ("GET", "/v1/models"): _resp(200, {"data": [{"id": "m"}]}),
    }))

    async def no_images(_sid):
        return False

    monkeypatch.setattr("llm.external_image_input.detect_image_input", no_images)
    monkeypatch.setattr(check, "detect_image_input", no_images)

    async def recover():
        server = nd.get_server(sid)
        if recovery == "health":
            async with httpx.AsyncClient() as wire:
                await nd._sweep_one(wire, server)
        else:
            result = await check.check_external_server(sid, server)
            assert result["ok"] is True

    asyncio.run(recover())
    before = _sources(client)[f"external:{sid}"]
    assert before["state"] == "ready"
    importlib.reload(local_sources)
    assert _sources(client)[f"external:{sid}"] == before


@pytest.mark.asyncio
async def test_busy_discovery_has_no_completed_receipt(monkeypatch):
    from llm import network_discovery as nd

    lock = asyncio.Lock()
    monkeypatch.setattr(nd, "_DISCOVERY_LOCKS", {"busy": lock})
    async with lock:
        receipt = await nd.discover_and_notify("busy")
    assert receipt["ok"] is False
    assert "already running" in receipt["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_enrichment_failure_preserves_core_discovery_receipt(monkeypatch, external):
    from llm import network_discovery as nd

    receipt = {"ok": True, "discovered": 2, "updated": 1}

    async def discovered(_sid):
        return receipt

    async def failed_enrichment(_target):
        raise OSError("optional enrichment unavailable")

    monkeypatch.setattr(nd, "_DISCOVERY_LOCKS", {})
    monkeypatch.setattr(nd, "get_server", lambda _sid: {
        "id": "enrichment", "kind": "external" if external else "managed"})
    monkeypatch.setattr(nd, "_notify_models_changed_safe", lambda: None)
    monkeypatch.setattr("llm.external_provider.discover_external_models", discovered)
    monkeypatch.setattr("llm.catalog.discover_network_models", discovered)
    monkeypatch.setattr("llm.external_image_input.detect_image_input", failed_enrichment)
    monkeypatch.setattr("llm.network_endpoints.build_api_base", lambda _sid: "http://test/v1")
    monkeypatch.setattr("llm.providers.omlx_introspection.warm_cache", failed_enrichment)
    assert await nd.discover_and_notify("enrichment") is receipt
