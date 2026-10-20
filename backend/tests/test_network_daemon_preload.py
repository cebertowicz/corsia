"""While oMLX preloads its pinned models, an unloaded pinned model reads as starting, not as not loaded."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from api.network_daemon_models import list_daemon_models
from llm import network_discovery as nd

STATUS = {"models": [
    {"id": "pinned-loaded", "loaded": True, "pinned": True, "estimated_size": 1},
    {"id": "pinned-waiting", "loaded": False, "pinned": True, "estimated_size": 1},
    {"id": "other", "loaded": False, "pinned": False, "estimated_size": 1},
]}


def _list(monkeypatch, health: httpx.Response | Exception, host: str = "127.0.0.1",
          status: dict = STATUS, health_calls: list[str] | None = None) -> dict:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models/status":
            return httpx.Response(200, json=status, request=request)
        if request.url.path == "/health":
            if health_calls is not None:
                health_calls.append(host)
            if isinstance(health, Exception):
                raise health
            return httpx.Response(health.status_code, json=health.json(), request=request)
        return httpx.Response(200, json={}, request=request)

    original = httpx.AsyncClient

    def client(*args, **kwargs) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    server_id = nd.upsert_server("omlx", f"{host}:5200", "Studio", host, 5200, "online")
    return asyncio.run(list_daemon_models(server_id, nd.get_server(server_id)))


def _starting(listing: dict) -> set[str]:
    return {m["id"] for m in listing["loaded"] + listing["available"] if m.get("starting")}


def test_an_unloaded_pin_is_starting_while_health_says_loading(isolated_db, monkeypatch) -> None:
    listing = _list(monkeypatch, httpx.Response(503, json={"status": "loading"}))

    assert _starting(listing) == {"pinned-waiting"}


@pytest.mark.parametrize("health", [
    httpx.Response(200, json={"status": "healthy"}),
    httpx.Response(503, json={"status": "error"}),
    httpx.ConnectError("down"),
])
def test_nothing_is_starting_once_health_is_not_loading(isolated_db, monkeypatch, health) -> None:
    assert _starting(_list(monkeypatch, health)) == set()


def test_each_row_carries_the_hosts_pin_and_memory_verdicts(isolated_db, monkeypatch) -> None:
    from infra import host_resources

    monkeypatch.setattr(host_resources, "pin_room_bytes", lambda: 1)
    monkeypatch.setattr(host_resources, "model_room_bytes", lambda: 0)

    listing = _list(monkeypatch, httpx.Response(200, json={"status": "healthy"}))
    rows = {m["id"]: m for m in listing["loaded"] + listing["available"]}

    assert rows["other"]["pin_fits"] is False
    assert rows["pinned-waiting"]["keep_line"] == "Not loaded: needs 1 GB, 0 GB free for a model now"
    assert "keep_line" not in rows["pinned-loaded"]


def test_a_server_on_another_computer_gets_no_verdict_from_this_computers_memory(isolated_db, monkeypatch) -> None:
    from infra import host_resources

    monkeypatch.setattr(host_resources, "pin_room_bytes", lambda: 1)
    monkeypatch.setattr(host_resources, "model_room_bytes", lambda: 0)

    listing = _list(monkeypatch, httpx.Response(200, json={"status": "healthy"}), host="192.168.1.20")

    assert all("pin_fits" not in m and "keep_line" not in m for m in listing["loaded"] + listing["available"])


def test_health_is_asked_only_when_a_pinned_model_is_waiting(isolated_db, monkeypatch) -> None:
    calls: list[str] = []
    quiet = {"models": [{"id": "other", "loaded": False, "pinned": False, "estimated_size": 1}]}

    _list(monkeypatch, httpx.Response(200, json={"status": "healthy"}), status=quiet, health_calls=calls)

    assert calls == []
