"""Startup refresh and provider discovery share one cost-map result."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import litellm
import pytest

from llm import catalog_cloud, external_provider, live_cost_map


@pytest.fixture
def discovery_store(monkeypatch):
    monkeypatch.setattr(catalog_cloud, "_provider_rows", lambda _provider: [])
    monkeypatch.setattr(
        catalog_cloud, "_persist_cloud_models",
        lambda _p, models, _now: [{**model, "id": model["provider_model_id"]} for model in models],
    )
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)


@pytest.mark.asyncio
async def test_pending_startup_refresh_does_not_block_start_and_discovery_uses_live_values(
    monkeypatch, discovery_store,
):
    entered = asyncio.Event()
    release = asyncio.Event()
    model = "claude-opus-5-5"
    old_map = {}
    new_map = {model: {"max_input_tokens": 1000000, "input_cost_per_token": 0.000005}}
    monkeypatch.setattr(litellm, "model_cost", old_map)
    monkeypatch.setattr(litellm, "add_known_models", lambda **_kw: None)
    monkeypatch.setattr(live_cost_map, "_invalidate_model_cost_lowercase_map", lambda: None)
    monkeypatch.setattr(live_cost_map.GetModelCostMap, "_get_backup_model_count", lambda: 0)
    monkeypatch.setattr(live_cost_map.GetModelCostMap, "validate_model_cost_map", lambda *_a: True)
    monkeypatch.setattr(external_provider, "fetch_external_models", AsyncMock(return_value=[model]))

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, url):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=new_map, request=httpx.Request("GET", url))

    monkeypatch.setattr(live_cost_map.httpx, "AsyncClient", Client)
    live_cost_map.start_refresh()
    await entered.wait()
    assert litellm.model_cost is old_map
    discovery = asyncio.create_task(catalog_cloud.discover_cloud_models("anthropic", "test-key"))
    await asyncio.sleep(0)
    assert not discovery.done()
    release.set()
    result = await discovery
    assert litellm.model_cost == new_map
    assert result["models"][0]["id"] == model
    assert result["models"][0]["context_window"] == 1000000
    assert result["models"][0]["input_cost_per_1m"] == 5


@pytest.mark.asyncio
async def test_failed_startup_refresh_keeps_dev_fallback(monkeypatch, discovery_store, caplog):
    model = "claude-sonnet-5-5"
    bundled = {}
    monkeypatch.setattr(litellm, "model_cost", bundled)
    monkeypatch.setattr(external_provider, "fetch_external_models", AsyncMock(return_value=[model]))

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, _url):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(live_cost_map.httpx, "AsyncClient", Client)
    live_cost_map.start_refresh()
    result = await catalog_cloud.discover_cloud_models("anthropic", "test-key")
    assert litellm.model_cost is bundled
    assert result["models"][0]["input_cost_per_1m"] == 0
    assert result["models"][0]["context_window"] is None
    assert sum("cost map refresh failed" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_anthropic_endpoint_pagination_includes_dated_and_unbundled_models(monkeypatch):
    seen = []
    pages = [
        {"data": [{"id": "claude-sonnet-4-6-20260101"}], "has_more": True, "last_id": "cursor"},
        {"data": [{"id": "claude-opus-5-5"}], "has_more": False},
    ]

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, url, **kwargs):
            seen.append((url, kwargs))
            return httpx.Response(200, json=pages.pop(0), request=httpx.Request("GET", url))

    monkeypatch.setattr(external_provider.httpx, "AsyncClient", Client)
    ids = await external_provider.fetch_external_models("https://api.anthropic.com", "anthropic", "test-key")
    assert ids == ["claude-sonnet-4-6-20260101", "claude-opus-5-5"]
    assert seen[0][0].endswith("/v1/models")
    assert seen[1][1]["params"] == {"after_id": "cursor"}
    assert [row["provider_model_id"] for row in catalog_cloud._anthropic_discovered(ids)] == ids


@pytest.mark.asyncio
async def test_anthropic_probe_http_failure_matches_openai(monkeypatch, discovery_store):
    request = httpx.Request("GET", "https://example.invalid/v1/models")
    response = httpx.Response(401, request=request)
    monkeypatch.setattr(
        external_provider, "fetch_external_models",
        AsyncMock(side_effect=httpx.HTTPStatusError("failed", request=request, response=response)),
    )
    assert await catalog_cloud.discover_cloud_models("anthropic", "test-key") == {
        "ok": False, "models": [], "error": "API returned 401",
    }
