from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from openai import AsyncOpenAI


def test_keyed_omlx_dispatch_uses_header_without_logging_token(monkeypatch, caplog):
    from llm import network_discovery, network_endpoints
    from llm.local_backends import OmlxBackend

    token = "test-keyed-omlx-dispatch"
    monkeypatch.setattr(network_endpoints, "build_api_base", lambda _sid: "http://omlx.test/v1")
    monkeypatch.setattr(network_discovery, "get_server_metadata", lambda _sid: {"api_key": token})
    caplog.set_level(logging.DEBUG)

    params = OmlxBackend().make_provider_params("omlx@server-a/model-a")

    assert params["omlx_server_id"] == "server-a"
    assert token not in caplog.text


def test_keyed_omlx_health_uses_header_for_loaded_model_check(monkeypatch, caplog):
    from llm import network_discovery, network_endpoints
    from llm.local_backends import OmlxBackend
    from llm.providers import omlx as omlx_provider

    token = "test-keyed-omlx-health"
    monkeypatch.setattr(network_discovery, "get_server", lambda _sid: {
        "id": "server-a", "instance_name": "oMLX", "port": 5201,
    })
    monkeypatch.setattr(network_discovery, "get_server_metadata", lambda _sid: {"api_key": token})

    async def fresh(_server_id, *, budget_s=1.0):
        return True

    async def check(_cfg):
        return {"running": True, "api_base": "http://omlx.test/v1"}

    received: dict[str, str] = {}

    async def loaded(_api_base, _model, *, server_id=None):
        received["server_id"] = server_id

    monkeypatch.setattr(network_endpoints, "freshness_guard", fresh)
    monkeypatch.setattr(omlx_provider, "check_server", check)
    monkeypatch.setattr(omlx_provider, "require_loaded_model", loaded)
    caplog.set_level(logging.DEBUG)

    healthy, reason = asyncio.run(OmlxBackend().check_health("omlx@server-a/model-a"))

    assert (healthy, reason) == (True, "")
    assert received == {"server_id": "server-a"}
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_keyed_omlx_native_transport_uses_current_key_from_cached_params(isolated_db, monkeypatch):
    """CORSIA's real LiteLLM path resolves the current registered key per request."""
    import llm.base as base
    from infra.db_async import run_in_db
    from llm import network_discovery as nd
    from llm.local_backends import OmlxBackend
    from llm.providers import omlx_lifecycle as lifecycle

    server_id = await run_in_db(
        nd.upsert_server,
        "omlx", "mock.invalid", "Proof", "mock.invalid", 5200, "online", kind="managed",
    )
    await run_in_db(nd.set_server_metadata, server_id, api_key="first")
    params = OmlxBackend().make_provider_params(f"omlx@{server_id}/proof-model")
    status_headers: list[str | None] = []
    requests: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def status_handler(request: httpx.Request) -> httpx.Response:
        status_headers.append(request.headers.get("authorization"))
        return httpx.Response(
            200, json={"models": [{"id": "proof-model", "loaded": True}]}, request=request,
        )

    class StatusClient(original_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(status_handler)
            super().__init__(*args, **kwargs)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    'data: {"id":"proof","object":"chat.completion.chunk",'
                    '"created":0,"model":"proof-model","choices":['
                    '{"index":0,"delta":{"role":"assistant","content":"ok"},'
                    '"finish_reason":null}]}\n\n'
                    'data: {"id":"proof","object":"chat.completion.chunk",'
                    '"created":0,"model":"proof-model","choices":['
                    '{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                    'data: [DONE]\n\n'
                ).encode(),
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "id": "proof", "object": "chat.completion", "created": 0,
                "model": "proof-model",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            request=request,
        )

    real_acompletion = base.acompletion

    async with original_client(transport=httpx.MockTransport(handler)) as http_client:
        sdk_client = AsyncOpenAI(
            api_key="omlx", base_url=params["api_base"],
            http_client=http_client, max_retries=0,
        )
        monkeypatch.setattr(lifecycle.httpx, "AsyncClient", StatusClient)

        async def native_acompletion(**kwargs):
            return await real_acompletion(**kwargs, client=sdk_client)

        monkeypatch.setattr(base, "acompletion", native_acompletion)
        response = await base.base_completion(
            [{"role": "user", "content": "proof"}],
            model=params["model"], api_key=params["api_key"],
            api_base=params["api_base"], omlx_server_id=params["omlx_server_id"],
            retry_transient_errors=False,
        )
        await run_in_db(nd.set_server_metadata, server_id, api_key="second")
        chunks: list[str] = []

        async def on_content(chunk):
            chunks.append(chunk)

        streamed = await base.base_completion(
            [{"role": "user", "content": "proof"}],
            model=params["model"], api_key=params["api_key"],
            api_base=params["api_base"], omlx_server_id=params["omlx_server_id"],
            on_content_chunk=on_content, retry_transient_errors=False,
        )

    assert response.content == "ok"
    assert streamed.content == "ok"
    assert chunks == ["ok"]
    assert [json.loads(request.content).get("stream", False) for request in requests] == [False, True]
    assert status_headers == ["Bearer first", "Bearer second"]
    assert [request.headers.get("authorization") for request in requests] == ["Bearer first", "Bearer second"]


def test_keyed_omlx_discovery_uses_header_for_all_authenticated_surfaces(monkeypatch, caplog):
    import llm.catalog_network_models as catalog

    token = "test-keyed-omlx-discovery"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/models/status":
            return httpx.Response(200, json={"models": []}, request=request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []}, request=request)
        if request.url.path == "/admin/api/status":
            return httpx.Response(200, json={}, request=request)
        return httpx.Response(404, request=request)

    original_client = httpx.AsyncClient

    class RoutedClient(original_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RoutedClient)
    caplog.set_level(logging.DEBUG)

    models, status = asyncio.run(catalog._fetch_omlx_canonical_models(
        "http://omlx.test/v1", "http://omlx.test/admin/api", token,
    ))

    assert models == []
    assert status == {}
    assert {request.url.path for request in requests} == {
        "/v1/models/status", "/v1/models", "/admin/api/status",
    }
    assert all(request.headers.get("authorization") == f"Bearer {token}" for request in requests)
    assert token not in caplog.text
