from __future__ import annotations

import asyncio
import time

import pytest


async def _heartbeat_delay() -> float:
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.sleep(0.01)
    return loop.time() - started


class _SlowConnection:
    def __init__(self, connection) -> None:
        self._connection = connection
        self._delayed = False

    def execute(self, *args, **kwargs):
        if not self._delayed:
            self._delayed = True
            time.sleep(0.2)
        return self._connection.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.mark.asyncio
async def test_managed_catalog_reconciliation_does_not_block_event_loop(
    isolated_db,
    monkeypatch,
):
    from infra import database
    from llm import catalog_network_models as catalog
    from llm import network_discovery as discovery
    from llm import network_endpoints as endpoints

    monkeypatch.setattr(
        discovery,
        "get_server",
        lambda server_id: {
            "id": server_id,
            "provider": "omlx",
            "instance_name": "oMLX",
            "status": "online",
            "api_key": None,
        },
    )
    monkeypatch.setattr(endpoints, "build_api_base", lambda _sid: "http://omlx/v1")
    monkeypatch.setattr(endpoints, "build_mgmt_base", lambda _sid: "http://omlx/admin/api")

    async def fetch(*_args, **_kwargs):
        return ([{"id": "Fortissimo", "loaded": True}], {})

    monkeypatch.setattr(catalog, "_fetch_omlx_canonical_models", fetch)
    monkeypatch.setattr("llm.reasoning_config.reasoning_config_for", lambda *_a: None)
    real_get_db = database.get_db
    monkeypatch.setattr(database, "get_db", lambda: _SlowConnection(real_get_db()))

    delay, result = await asyncio.gather(
        _heartbeat_delay(),
        catalog.discover_network_models("srv-test"),
    )

    assert result["ok"] is True
    assert delay < 0.1


@pytest.mark.asyncio
async def test_external_catalog_reconciliation_does_not_block_event_loop(
    isolated_db,
    monkeypatch,
):
    from infra import database
    from llm import external_provider as provider
    from llm import network_discovery as discovery
    from llm import network_endpoints as endpoints

    monkeypatch.setattr(
        discovery,
        "get_server",
        lambda server_id: {
            "id": server_id,
            "provider": "external",
            "instance_name": "External",
            "status": "online",
            "dialect": "openai",
        },
    )
    monkeypatch.setattr(discovery, "get_server_metadata", lambda _sid: {})
    monkeypatch.setattr(endpoints, "build_api_base", lambda _sid: "http://external/v1")

    async def fetch(*_args, **_kwargs):
        return ["qwen"]

    monkeypatch.setattr(provider, "fetch_external_models", fetch)
    monkeypatch.setattr(
        provider,
        "_capability_overrides",
        lambda _server: {
            "context_window": None,
            "supports_vision": 0,
            "supports_tools": 1,
            "supports_reasoning": 0,
        },
    )
    real_get_db = database.get_db
    monkeypatch.setattr(database, "get_db", lambda: _SlowConnection(real_get_db()))

    delay, result = await asyncio.gather(
        _heartbeat_delay(),
        provider.discover_external_models("srv-test"),
    )

    assert result["ok"] is True
    assert delay < 0.1
