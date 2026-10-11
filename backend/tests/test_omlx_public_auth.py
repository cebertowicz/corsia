from __future__ import annotations

import asyncio

import httpx
import pytest

from infra.key_store_servers import remove_server_key, set_server_key


def _register(host: str, *, port: int = 5200, key: str | None = None) -> str:
    from llm import network_discovery as nd

    server_id = nd.upsert_server(
        "omlx", host, host, host, port, "offline", kind="managed",
    )
    if key is not None:
        set_server_key(server_id, key)
    return server_id


def test_registered_public_headers_read_current_metadata_for_rotation_and_removal(isolated_db):
    from llm import network_discovery as nd
    from llm.providers.omlx_public_auth import public_headers

    server_id = _register("one.test", key="first")
    url = "http://one.test:5200/v1/models"

    assert asyncio.run(public_headers({"server_id": server_id}, url=url)) == {
        "Authorization": "Bearer first",
    }
    set_server_key(server_id, "second")
    assert asyncio.run(public_headers({"server_id": server_id}, url=url)) == {
        "Authorization": "Bearer second",
    }
    remove_server_key(server_id)
    assert asyncio.run(public_headers({"server_id": server_id}, url=url)) == {}


def test_registered_public_headers_accept_known_alternates_and_refuse_unregistered_origins(isolated_db):
    from llm import network_endpoints as ne
    from llm.providers.omlx_public_auth import OmlxPublicAuthError, public_headers

    server_id = _register("127.0.0.1", key="one-key")
    ne.upsert_endpoint(server_id, "192.168.1.20", ok=False)

    assert asyncio.run(public_headers(
        {"server_id": server_id}, url="http://192.168.1.20:5200/v1/models",
    )) == {"Authorization": "Bearer one-key"}
    with pytest.raises(OmlxPublicAuthError):
        asyncio.run(public_headers(
            {"server_id": server_id}, url="http://unregistered.test:5200/v1/models",
        ))


def test_unregistered_public_headers_are_keyless_or_candidate_only(isolated_db):
    from llm.providers.omlx_public_auth import public_headers

    assert asyncio.run(public_headers({}, url="http://candidate.test/v1/models")) == {}
    assert asyncio.run(public_headers(
        {"api_key": "candidate"}, url="http://candidate.test/v1/models",
    )) == {"Authorization": "Bearer candidate"}
    assert asyncio.run(public_headers(
        {"server_id": "unknown", "api_key": "candidate"},
        url="http://candidate.test/v1/models",
    )) == {}


def test_registered_sweep_recovers_on_second_known_endpoint(isolated_db):
    from llm import network_discovery as nd
    from llm import network_endpoints as ne

    server_id = _register("127.0.0.1", key="disposable-proof")
    ne.upsert_endpoint(server_id, "192.168.1.20", ok=False)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "127.0.0.1":
            return httpx.Response(503, request=request)
        authorized = request.headers.get("authorization") == "Bearer disposable-proof"
        return httpx.Response(
            200 if authorized else 401, json={"status": "ok", "models": []}, request=request,
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await nd._sweep_managed(client, nd.get_server(server_id))

    asyncio.run(run())

    assert "192.168.1.20" in seen
    assert nd.get_server(server_id)["status"] == "online"


def test_freshness_guard_authenticates_each_registered_managed_endpoint(isolated_db, monkeypatch):
    from llm import network_endpoints as ne

    server_id = _register("127.0.0.1", key="disposable-proof")
    original = httpx.AsyncClient
    sent: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorized = request.headers.get("authorization") == "Bearer disposable-proof"
        sent.append(authorized)
        return httpx.Response(200 if authorized else 401, json={"status": "ok"}, request=request)

    class Client(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)

    assert asyncio.run(ne.freshness_guard(server_id)) is True
    assert sent and all(sent)


def test_readiness_reloads_registered_key_before_each_probe(isolated_db, monkeypatch):
    from llm import network_discovery as nd
    from llm.providers import omlx_supervisor as supervisor

    server_id = _register("127.0.0.1", key="first")
    original = httpx.AsyncClient
    headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers.get("authorization"))
        if len(headers) == 1:
            set_server_key(server_id, "second")
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"data": []}, request=request)

    class Client(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(supervisor.httpx, "AsyncClient", Client)
    monkeypatch.setattr(supervisor, "HEALTH_POLL_INTERVAL_S", 0)

    assert asyncio.run(supervisor._wait_for_healthy(
        "127.0.0.1", 5200, 1, server_id=server_id,
    )) is True
    assert headers == ["Bearer first", "Bearer second"]
