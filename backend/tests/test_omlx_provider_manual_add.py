from __future__ import annotations

import asyncio

import pytest

from llm.providers import omlx
from omlx_provider_fixtures import _patch_httpx, _resp, _Router


def test_omlx_manual_add_probes_health_and_v1_models(monkeypatch):
    import asyncio
    from api.network_servers import _add_omlx_server, AddServerBody

    router = _Router({
        ("GET", "/health"): _resp(200, {"status": "ok"}),
        ("GET", "/v1/models"): _resp(200, {"data": [{"id": "x"}]}),
    })
    _patch_httpx(monkeypatch, router)

    upserted: list = []
    stored: list = []
    from api import network_server_helpers
    from llm import network_discovery
    monkeypatch.setattr(
        network_discovery, "upsert_server",
        lambda **kw: (upserted.append(kw), "ns-omlx-1")[1],
    )
    monkeypatch.setattr(
        network_server_helpers, "set_server_key",
        lambda sid, key, *, db: stored.append((sid, key)),
    )

    body = AddServerBody(
        name="Studio", hostname="127.0.0.1", port=8000,
        provider="omlx", api_key="sk-test-key",
    )
    out = asyncio.run(_add_omlx_server(body))
    assert out == {"ok": True, "server_id": "ns-omlx-1", "status": "online"}
    paths = [u for _, u, _ in router.calls]
    assert any("/health" in p for p in paths)
    assert any("/v1/models" in p for p in paths)
    assert len(upserted) == 1
    assert upserted[0]["provider"] == "omlx"
    assert upserted[0]["instance_id"] == "127.0.0.1:8000"
    assert upserted[0]["source"] == "manual"
    assert stored == [("ns-omlx-1", "sk-test-key")]


def test_omlx_manual_add_rejects_when_health_fails(monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from api.network_servers import _add_omlx_server, AddServerBody

    router = _Router({("GET", "/health"): _resp(503)})
    _patch_httpx(monkeypatch, router)
    body = AddServerBody(name="x", hostname="h", port=1, provider="omlx")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_add_omlx_server(body))
    assert exc.value.status_code == 400
    assert "/health" in exc.value.detail


def test_omlx_manual_add_rejects_when_v1_models_fails(monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from api.network_servers import _add_omlx_server, AddServerBody

    router = _Router({
        ("GET", "/health"): _resp(200, {"status": "ok"}),
        ("GET", "/v1/models"): _resp(500),
    })
    _patch_httpx(monkeypatch, router)
    body = AddServerBody(name="x", hostname="h", port=1, provider="omlx")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_add_omlx_server(body))
    assert exc.value.status_code == 400
    assert "/v1/models" in exc.value.detail


def test_omlx_manual_add_skips_metadata_when_no_api_key(monkeypatch):
    import asyncio
    from api.network_servers import _add_omlx_server, AddServerBody

    router = _Router({
        ("GET", "/health"): _resp(200, {"status": "ok"}),
        ("GET", "/v1/models"): _resp(200, {"data": []}),
    })
    _patch_httpx(monkeypatch, router)

    metadata_calls: list = []
    from llm import network_discovery
    monkeypatch.setattr(network_discovery, "upsert_server", lambda **kw: "ns-2")
    monkeypatch.setattr(
        network_discovery, "set_server_metadata",
        lambda sid, **kw: metadata_calls.append((sid, kw)),
    )

    body = AddServerBody(name="x", hostname="h", port=1, provider="omlx", api_key=None)
    asyncio.run(_add_omlx_server(body))
    assert metadata_calls == []



def test_resolve_endpoint_uses_rich_cfg_directly():
    h, p = omlx._resolve_endpoint({"host": "127.0.0.1", "port": 8000})
    assert (h, p) == ("127.0.0.1", 8000)


def test_resolve_endpoint_looks_up_from_server_id_when_sparse(monkeypatch):
    from llm import network_discovery, network_endpoints
    monkeypatch.setattr(
        network_discovery, "get_server",
        lambda sid: {"id": sid, "port": 8000} if sid == "ns-1" else None,
    )
    monkeypatch.setattr(
        network_endpoints, "get_active_endpoint",
        lambda sid: {"host": "Studio.local"} if sid == "ns-1" else None,
    )
    h, p = omlx._resolve_endpoint({"server_id": "ns-1"})
    assert (h, p) == ("Studio.local", 8000)
