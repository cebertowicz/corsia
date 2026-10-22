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
          status: dict = STATUS, health_calls: list[str] | None = None, *,
          own: bool = True, failed: dict[str, str] | None = None) -> dict:
    """List a server's models: CORSIA's own oMLX by default, else a person's legacy managed-kind oMLX."""
    from llm import local_keep_loaded
    from llm.managed_omlx_binary import MANAGED_INSTANCE_ID

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
    server_id = nd.upsert_server("omlx", MANAGED_INSTANCE_ID if own else f"{host}:5200", "Studio", host, 5200,
                                 "online", kind="managed", source="corsia" if own else "manual")
    monkeypatch.setattr(local_keep_loaded, "_FAILED", {(server_id, model): line for model, line in (failed or {}).items()})
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


@pytest.mark.parametrize("host", ["127.0.0.1", "192.168.1.20"])
def test_a_persons_own_omlx_gets_no_keep_loaded_verdict_here_or_elsewhere(isolated_db, monkeypatch, host) -> None:
    """Only CORSIA's own oMLX is ever pinned, so only its rows get pin_fits or a keep line."""
    from infra import host_resources

    monkeypatch.setattr(host_resources, "pin_room_bytes", lambda: 1)
    monkeypatch.setattr(host_resources, "model_room_bytes", lambda: 0)

    listing = _list(monkeypatch, httpx.Response(200, json={"status": "healthy"}), host=host, own=False)

    assert all("pin_fits" not in m and "keep_line" not in m for m in listing["loaded"] + listing["available"])


def test_a_failed_keep_attempt_from_setup_or_settings_shows_on_its_row(isolated_db, monkeypatch) -> None:
    from infra import host_resources
    from llm.local_keep_loaded import NOT_KEPT

    monkeypatch.setattr(host_resources, "pin_room_bytes", lambda: 10)
    monkeypatch.setattr(host_resources, "model_room_bytes", lambda: 10)

    listing = _list(monkeypatch, httpx.Response(200, json={"status": "healthy"}), failed={"other": NOT_KEPT})
    rows = {m["id"]: m for m in listing["loaded"] + listing["available"]}

    assert rows["other"]["keep_line"] == NOT_KEPT
    assert "keep_line" not in rows["pinned-waiting"]


def test_health_is_asked_only_when_a_pinned_model_is_waiting(isolated_db, monkeypatch) -> None:
    calls: list[str] = []
    quiet = {"models": [{"id": "other", "loaded": False, "pinned": False, "estimated_size": 1}]}

    _list(monkeypatch, httpx.Response(200, json={"status": "healthy"}), status=quiet, health_calls=calls)

    assert calls == []
