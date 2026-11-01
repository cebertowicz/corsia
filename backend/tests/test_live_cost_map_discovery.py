"""Startup refresh and provider discovery share one cost-map result."""

import asyncio
import json
from importlib.resources import files

import httpx
import litellm
import pytest

from infra.database import get_db
from llm import catalog_cloud, live_cost_map


@pytest.mark.asyncio
async def test_bundled_map_refresh_uses_real_litellm_functions(monkeypatch):
    backup = json.loads(files("litellm").joinpath("model_prices_and_context_window_backup.json").read_text())
    model = next(key for key, value in backup.items() if value.get("litellm_provider") == "openai")
    original = litellm.model_cost
    monkeypatch.setattr(litellm, "model_cost", original)
    monkeypatch.setattr(
        litellm, "open_ai_chat_completion_models", set(litellm.open_ai_chat_completion_models) - {model},
    )
    client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=backup, request=request))
    monkeypatch.setattr(live_cost_map.httpx, "AsyncClient", lambda **kwargs: client(transport=transport, **kwargs))

    await live_cost_map._refresh()

    assert litellm.model_cost is not original
    assert model in litellm.model_cost
    assert model in litellm.open_ai_chat_completion_models


@pytest.mark.asyncio
async def test_a_malformed_map_keeps_the_bundled_map_and_releases_waiters(monkeypatch, caplog):
    backup = json.loads(files("litellm").joinpath("model_prices_and_context_window_backup.json").read_text())
    bundled = litellm.model_cost
    monkeypatch.setattr(litellm, "model_cost", bundled)
    client = httpx.AsyncClient
    payload = {**backup, "broken": "not an entry"}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
    monkeypatch.setattr(live_cost_map.httpx, "AsyncClient", lambda **kwargs: client(transport=transport, **kwargs))

    live_cost_map.start_refresh()
    await live_cost_map.wait_for_refresh()

    assert litellm.model_cost is bundled
    assert any("refresh broke" in record.message and record.exc_info for record in caplog.records)


@pytest.mark.asyncio
async def test_a_stalled_fetch_keeps_the_bundled_map_and_releases_waiters(monkeypatch, caplog):
    bundled = litellm.model_cost
    monkeypatch.setattr(litellm, "model_cost", bundled)
    monkeypatch.setattr(live_cost_map, "_REFRESH_TIMEOUT", 0.01)

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, _url):
            await asyncio.Event().wait()

    monkeypatch.setattr(live_cost_map.httpx, "AsyncClient", Client)

    live_cost_map.start_refresh()
    await asyncio.wait_for(live_cost_map.wait_for_refresh(), timeout=5)

    assert litellm.model_cost is bundled
    assert any("cost map refresh failed" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_pending_refresh_makes_unbundled_anthropic_model_discoverable(monkeypatch, isolated_db):
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
    monkeypatch.setattr("llm.hosted_catalog.cloud_models.reasoning_shape_for", lambda *_a: None)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)

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
async def test_failed_refresh_preserves_missing_stored_model_and_skips_dated_ids(
    monkeypatch, isolated_db, caplog,
):
    model = "claude-sonnet-5-5"
    dated = "claude-sonnet-4-5-20250929"
    bundled = {dated: {"max_input_tokens": 200000}}
    monkeypatch.setattr(litellm, "model_cost", bundled)
    monkeypatch.setattr("llm.hosted_catalog.cloud_models.reasoning_shape_for", lambda *_a: None)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)
    catalog_cloud._persist_cloud_models("anthropic", [{
        "provider_model_id": model, "litellm_model": f"anthropic/{model}",
        "display_name": model, "featured": True, "input_cost_per_1m": 3,
        "output_cost_per_1m": 15, "context_window": 500000,
        "max_output_tokens": 16000, "supports_vision": True,
        "supports_tools": True, "supports_reasoning": False,
    }], "before")

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
    assert result["ok"] and result["models"] == []
    assert litellm.model_cost is bundled
    rows = get_db().execute(
        "SELECT provider_model_id, context_window, metadata_json FROM models "
        "WHERE provider = ? AND provider_model_id IN (?, ?)",
        ("anthropic", model, dated),
    ).fetchall()
    assert len(rows) == 1 and rows[0]["provider_model_id"] == model
    assert rows[0]["context_window"] == 500000
    metadata = json.loads(rows[0]["metadata_json"])
    assert metadata["input_cost_per_1m"] == 3
    assert metadata["output_cost_per_1m"] == 15
    assert sum("cost map refresh failed" in r.message for r in caplog.records) == 1
