"""External catalog journeys never run any of the listed models."""

from __future__ import annotations

import asyncio

import pytest

from omlx_provider_fixtures import _patch_httpx, _resp, _Router


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
@pytest.mark.parametrize("count", [10, 100])
@pytest.mark.parametrize("list_status", [200, 404, 405])
async def test_catalog_journeys_only_read_metadata(isolated_db, monkeypatch, dialect, count, list_status):
    from api import external_connection_check as check, network_server_helpers as helpers
    from api.network_servers import AddServerBody, UpdateExternalServerBody
    from llm import external_image_input as images, network_discovery as nd, router
    from llm.external_catalog_view import external_catalog_models

    listed = [{"id": f"model-{i:03}", "max_model_len": 32768} for i in range(count)]
    wire = _Router({
        ("GET", "/v1/models"): _resp(list_status, {"data": listed}),
        ("POST", "/v1/chat/completions"): _resp(200),
        ("POST", "/v1/messages"): _resp(200),
    })
    _patch_httpx(monkeypatch, wire)
    inferred = []

    async def completion(*args, **kwargs):
        inferred.append(kwargs)
        raise AssertionError("catalog discovery must not run a model")

    monkeypatch.setattr(router, "chat_completion", completion)
    monkeypatch.setattr(nd, "_notify_models_changed_safe", lambda: None)
    tasks = []
    create_task = asyncio.create_task

    def capture(coro, *args, **kwargs):
        task = create_task(coro, *args, **kwargs)
        tasks.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", capture)

    async def drain():
        while tasks:
            pending, tasks[:] = list(tasks), []
            await asyncio.gather(*pending)

    added = await helpers.add_external_server(AddServerBody(
        name="Local lab", base_url="http://127.0.0.1:8000/v1", dialect=dialect,
        model_ids=[m["id"] for m in listed] if list_status != 200 else None,
    ))
    sid = added["server_id"]
    await drain()
    models = external_catalog_models(sid)["loaded"]
    assert len(models) == count
    assert all(m["image_input"] is None for m in models)
    # Discovery and connection checks must preserve the person's one-model choice.
    assert await images.set_image_input(sid, "model-000", "yes")
    await helpers.update_external_server(sid, UpdateExternalServerBody(
        base_url="http://127.0.0.1:8000/v1", dialect=dialect,
    ))
    await drain()
    assert (await nd.discover_and_notify(sid))["ok"]
    result = await check.check_external_server(sid, nd.get_server(sid))
    await drain()

    assert result["ok"]
    assert f"{count} models found" in result["message"]
    assert "Not determined:" in result["message"]
    assert inferred == []
    assert wire.calls and all(method == "GET" for method, _url, _body in wire.calls)
    chosen = external_catalog_models(sid)["loaded"][0]
    assert chosen["image_input"]["source"] == "user"
    assert chosen["vision"] is True
