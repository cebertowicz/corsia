"""Regression tests for the revised unloaded-model send approach."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from infra import host_resources
from llm import local_model_load
from llm.local_model_load import Readiness, ensure_loaded, readiness, seat_lock
from test_local_model_load import SERVER, _target, lifecycle, seat  # noqa: F401


def test_readiness_projects_send_admission(seat):
    seat.model("target", 10)
    db = seat.connect()
    try:
        assert readiness(_target(), db=db) == Readiness("fits_now", 0)
        seat.loaded.add("target")
        assert readiness(_target(), db=db) == Readiness("loaded", 0)
        unavailable = replace(_target(), available=False, availability_reason="model_unavailable")
        assert readiness(unavailable, db=db) == Readiness("loaded", 0)
        assert readiness(_target(provider="external"), db=db) is None
        assert readiness(replace(_target(), transport_model=f"external@{SERVER}/target"), db=db) is None
        seat.status("offline")
        assert readiness(_target(), db=db) == Readiness("unreachable", 0)
    finally:
        db.close()


@pytest.mark.parametrize("unknown", ["target", "other", "remote"])
@pytest.mark.parametrize("other_loaded", [False, True])
def test_unknown_fit_requires_empty_seat(seat, unknown, other_loaded):
    seat.model("target", None if unknown == "target" else 10)
    if other_loaded:
        seat.model("other", None if unknown == "other" else 5, loaded=True)
    if unknown == "remote":
        seat.on_this_mac = False
    # Without another loaded model there is no unknown loaded size to judge.
    expected = "no_room" if other_loaded else "fits_now"
    assert seat.readiness() == Readiness(expected, int(other_loaded))


@pytest.mark.asyncio
async def test_live_recheck_avoids_loading_when_cache_is_stale(seat, lifecycle, monkeypatch):
    seat.model("target", 10)
    calls = 0

    async def live(_cfg):
        nonlocal calls
        calls += 1
        return [{"identifier": "target"}]

    from llm.providers import omlx_lifecycle
    monkeypatch.setattr(omlx_lifecycle, "read_loaded", live, raising=False)
    await ensure_loaded(_target())
    assert calls == 1
    assert seat.loads == []


@pytest.mark.asyncio
async def test_live_status_loads_even_when_cache_says_loaded(seat, lifecycle, monkeypatch):
    from llm.base import _omlx_loaded_only_preflight
    from llm.providers import omlx_lifecycle

    seat.model("target", 10, loaded=True)
    actual = set()

    async def live(_cfg):
        return [{"identifier": name} for name in actual]

    async def load(_cfg, model):
        seat.loads.append(model)
        actual.add(model)
        return {"ok": True}

    async def require(_base, model, *, server_id=None):
        assert model in actual

    monkeypatch.setattr(omlx_lifecycle, "read_loaded", live, raising=False)
    monkeypatch.setattr(omlx_lifecycle, "load_model", load)
    monkeypatch.setattr(omlx_lifecycle, "require_loaded_model", require)
    await ensure_loaded(_target())
    assert seat.loads == ["target"]
    assert await _omlx_loaded_only_preflight("omlx", "http://localhost/v1", "target", SERVER) is None


@pytest.mark.parametrize("status,payload,expected", [
    (200, {"models": []}, []),
    (200, {"models": "invalid"}, None),
    (500, {}, None),
])
@pytest.mark.asyncio
async def test_live_status_distinguishes_unknown_from_empty(monkeypatch, status, payload, expected):
    from llm.providers import omlx_lifecycle
    from omlx_provider_fixtures import CFG_PUBLIC, _patch_httpx, _resp, _Router

    _patch_httpx(monkeypatch, _Router({("GET", "/v1/models/status"): _resp(status, payload)}))
    assert await omlx_lifecycle.read_loaded(CFG_PUBLIC) == expected
    assert await omlx_lifecycle.list_loaded(CFG_PUBLIC) == (expected or [])


@pytest.mark.asyncio
async def test_unknown_loaded_entry_preserves_rows_but_refuses_load(seat, monkeypatch):
    from llm.providers import omlx_lifecycle
    from omlx_provider_fixtures import CFG_PUBLIC, _patch_httpx, _resp, _Router

    seat.model("target", 10)
    router = _Router({("GET", "/v1/models/status"): _resp(200, {"models": [
        {"id": "known", "loaded": True}, {"id": "unknown", "loaded": None},
    ]})})
    _patch_httpx(monkeypatch, router)
    monkeypatch.setattr(omlx_lifecycle, "_api_base", lambda _cfg: "http://127.0.0.1:8000/v1")
    monkeypatch.setattr(omlx_lifecycle, "public_headers", AsyncMock(return_value={}))
    load = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(omlx_lifecycle, "load_model", load)
    assert [row["identifier"] for row in await omlx_lifecycle.list_loaded(CFG_PUBLIC)] == ["known"]
    assert await omlx_lifecycle.read_loaded(CFG_PUBLIC) is None
    with pytest.raises(local_model_load.ModelLoadFailed) as caught:
        await ensure_loaded(_target())
    assert caught.value.cause == "unreachable"
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_live_status_does_not_load(seat, lifecycle, monkeypatch):
    seat.model("target", 10)
    from llm.providers import omlx_lifecycle
    monkeypatch.setattr(omlx_lifecycle, "read_loaded", AsyncMock(return_value=None), raising=False)
    with pytest.raises(local_model_load.ModelLoadFailed) as caught:
        await ensure_loaded(_target())
    assert caught.value.cause == "unreachable"
    assert seat.loads == []


@pytest.mark.asyncio
async def test_send_load_emits_completion(seat, lifecycle, monkeypatch):
    seat.model("target", 10)
    events = []
    monkeypatch.setattr("infra.product_events.emit_product_event", lambda category, action, **kw: events.append((category, action)))
    await ensure_loaded(_target())
    assert ("model", "load.completed") in events


@pytest.mark.asyncio
async def test_settings_load_verdict_uses_same_room(monkeypatch):
    async def resources():
        return {"free_gb": 40, "safe_free_gb": 30, "safe_reserve_gb": 10}

    monkeypatch.setattr(host_resources, "get_host_resources", resources)
    monkeypatch.setattr(host_resources, "model_room_bytes", lambda: 2 * host_resources.GIB)
    result = await host_resources.check_can_load(3, "omlx")
    verdict = host_resources.load_verdict(3 * host_resources.GIB, [], 2 * host_resources.GIB)
    assert result["ok"] == (verdict == "fits_now")


@pytest.mark.asyncio
async def test_person_load_waits_for_same_seat_lock(seat, monkeypatch):
    from api import network_servers

    provider = SimpleNamespace(load_model=AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(network_servers.nd, "get_server", lambda _id: {"kind": "omlx", "provider": "omlx"})
    monkeypatch.setattr(network_servers, "for_provider", lambda _name: provider)
    monkeypatch.setattr(network_servers, "emit_product_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(network_servers.nd, "discover_and_notify", AsyncMock())
    async with seat_lock(SERVER):
        task = asyncio.create_task(network_servers.load_model(SERVER, network_servers.ModelBody(model="target")))
        await asyncio.sleep(0.05)
        assert not provider.load_model.called
    await task
    provider.load_model.assert_awaited_once()
