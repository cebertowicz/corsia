"""A managed oMLX server that comes back keeps being rediscovered until one discovery lists it.

Going offline marks every model row unavailable; each later reach rediscovers
until one discovery lists the models. A retry that fails the same way is not
logged or pushed to clients again. External servers are not retried.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import httpx
import pytest


@pytest.fixture(autouse=True)
def _no_relist_marks(monkeypatch):
    from llm import server_observation

    # Successful rediscovery also introspects, independently of notifications.
    monkeypatch.setattr('llm.providers.omlx_introspection.warm_cache', AsyncMock())
    server_observation._UNLISTED.clear()
    yield
    server_observation._UNLISTED.clear()


def _count_pushes(monkeypatch) -> list[int]:
    from llm import network_discovery as nd

    pushes: list[int] = []
    monkeypatch.setattr(nd, "_notify_models_changed_safe", lambda: pushes.append(1))
    return pushes


def _register() -> str:
    from llm import network_discovery as nd

    return nd.upsert_server("omlx", "127.0.0.1", "127.0.0.1", "127.0.0.1", 5200, "online", kind="managed")


async def _sweep(server_id: str, *, up: bool) -> None:
    from llm import network_discovery as nd
    from llm import server_observation

    def handler(request: httpx.Request) -> httpx.Response:
        if not up:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"status": "ok", "loaded_models": []}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await nd._sweep_managed(client, nd.get_server(server_id))
    while server_observation._PENDING:
        await asyncio.gather(*list(server_observation._PENDING))


def test_a_failed_rediscovery_is_retried_on_the_next_reach_until_one_lists(isolated_db, monkeypatch, caplog):
    from llm import catalog

    failed = {"ok": False, "error": "Connection failed: timed out"}
    answers = [failed, failed, {"ok": True}]
    calls: list[str] = []

    async def discover(server_id: str) -> dict:
        calls.append(server_id)
        return answers.pop(0) if answers else {"ok": True}

    monkeypatch.setattr(catalog, "discover_network_models", discover)
    pushes = _count_pushes(monkeypatch)
    server_id = _register()

    push_counts = []

    async def run() -> None:
        await _sweep(server_id, up=False)  # offline: rows marked unavailable
        push_counts.append(len(pushes))
        await _sweep(server_id, up=True)  # back: observed empty loaded set, discovery fails
        push_counts.append(len(pushes))
        await _sweep(server_id, up=True)  # still reachable: discovery runs again, fails the same way
        push_counts.append(len(pushes))
        await _sweep(server_id, up=True)  # and again, and lists
        push_counts.append(len(pushes))
        await _sweep(server_id, up=True)  # listed: nothing more to do
        push_counts.append(len(pushes))

    with caplog.at_level(logging.WARNING, logger="llm.server_observation"):
        asyncio.run(run())

    assert calls == [server_id, server_id, server_id]
    # Logged once for the repeated error, not on every sweep.
    assert caplog.text.count(f"discovery of {server_id} listed no models: Connection failed: timed out") == 1
    # Pushed on going offline, the first failure and the listing; not the repeat.
    # A missing observed set and an empty set describe the same loaded state.
    assert push_counts == [1, 2, 2, 3, 3]
    assert len(pushes) == 3


def test_a_failed_startup_discovery_is_retried_on_the_next_reach(isolated_db, monkeypatch):
    from llm import catalog
    from llm import network_discovery as nd

    answers = [{"ok": False, "error": "Connection failed: refused"}, {"ok": True}]
    calls: list[str] = []

    async def discover(server_id: str) -> dict:
        calls.append(server_id)
        return answers.pop(0)

    monkeypatch.setattr(catalog, "discover_network_models", discover)
    server_id = _register()

    async def run() -> None:
        await nd._discover_and_notify(server_id)  # startup discovery fails; never offline
        await _sweep(server_id, up=True)  # reached: discovery runs again and lists
        await _sweep(server_id, up=True)  # listed: nothing more to do

    asyncio.run(run())

    assert calls == [server_id, server_id]


def test_a_raised_discovery_marks_the_server_for_a_retry(isolated_db, monkeypatch):
    from llm import catalog
    from llm import network_discovery as nd
    from llm import server_observation

    async def discover(server_id: str) -> dict:
        raise RuntimeError("listing broke")

    monkeypatch.setattr(catalog, "discover_network_models", discover)
    server_id = _register()

    result = asyncio.run(nd._discover_and_notify(server_id))

    assert result == {"ok": False, "error": "listing broke"}
    assert server_observation._UNLISTED[server_id] == "listing broke"


def test_an_external_server_keeps_its_own_discovery_record(isolated_db, monkeypatch):
    from llm import external_provider
    from llm import network_discovery as nd
    from llm import server_observation

    async def discover(server_id: str) -> dict:
        return {"ok": False, "error": "401 Unauthorized"}

    monkeypatch.setattr(external_provider, "discover_external_models", discover)
    pushes = _count_pushes(monkeypatch)
    server_id = nd.upsert_server(
        provider="external", instance_id="lan.example/v1", instance_name="External",
        host="lan.example", port=8000, status="online", kind="external", dialect="openai",
    )

    async def run() -> None:
        await server_observation.record_unreachable(nd.get_server(server_id))
        await nd._discover_and_notify(server_id)

    asyncio.run(run())

    assert server_id not in server_observation._UNLISTED
    assert len(pushes) == 2  # going offline, then the failed discovery, as before


def test_a_server_that_never_went_offline_is_not_rediscovered_on_every_reach(isolated_db, monkeypatch):
    from llm import catalog

    calls: list[str] = []

    async def discover(server_id: str) -> dict:
        calls.append(server_id)
        return {"ok": True}

    monkeypatch.setattr(catalog, "discover_network_models", discover)
    server_id = _register()

    async def run() -> None:
        await _sweep(server_id, up=True)
        await _sweep(server_id, up=True)

    asyncio.run(run())

    assert calls == []
