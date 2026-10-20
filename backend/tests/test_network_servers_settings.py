
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

from auth_fixtures import seed_account
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TOKEN = "test-token-01"


@pytest.fixture
def client(tmp_path, monkeypatch):
    seed_account("u-test", _TOKEN)

    from llm import network_discovery as _nd
    monkeypatch.setattr(
        _nd,
        "get_server",
        lambda sid: {"id": sid, "provider": "omlx", "host": "127.0.0.1", "port": 5100}
        if sid == "ns-test"
        else None,
    )

    from api.network_servers import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)



@pytest.mark.parametrize(
    "method,path",
    [
        ("get", ""),
        ("post", ""),
        ("get", "/ns-test"),
        ("delete", "/ns-test"),
        ("get", "/ns-test/models"),
        ("post", "/ns-test/models/load"),
        ("post", "/ns-test/models/unload"),
        ("get", "/ns-test/settings"),
        ("post", "/ns-test/lifecycle/stop"),
        ("get", "/ns-test/models/m/runtime-info"),
        ("put", "/ns-test/models/m/settings"),
        ("get", "/ns-test/cache/status"),
        ("get", "/ns-test/jobs/active"),
        ("get", "/ns-test/jobs/j1"),
        ("delete", "/ns-test/jobs/j1"),
    ],
)
@pytest.mark.parametrize("query", ["", "?token=not-a-real-credential"])
def test_every_route_refuses_a_missing_or_unknown_credential(client, method, path, query):
    # The backend listens on 0.0.0.0, so these routes must not act for an
    # anonymous caller on the local network.
    with patch("llm.providers.omlx.get_settings", new=AsyncMock()) as provider:
        r = client.request(method, f"/api/settings/network-servers{path}{query}")
    assert r.status_code == 401
    provider.assert_not_awaited()


def test_get_settings_unknown_server_404s(client):
    r = client.get(f"/api/settings/network-servers/ns-missing/settings?token={_TOKEN}")
    assert r.status_code == 404
    assert "Server not found" in r.json()["detail"]


def test_get_settings_returns_daemon_payload(client):
    payload = {
        "ok": True,
        "server": {"host": "127.0.0.1", "port": 5100, "log_level": "INFO"},
        "sampling_defaults": {"temperature": 0.7},
        "sampling_clamps": {"temperature": [0.0, 5.0]},
        "metadata": {"boot_only": ["host", "port"], "hot_applicable": ["log_level"], "computed": []},
    }
    with patch("llm.providers.omlx.get_settings", new=AsyncMock(return_value=payload)):
        r = client.get(f"/api/settings/network-servers/ns-test/settings?token={_TOKEN}")
    assert r.status_code == 200
    body = r.json()
    assert "ok" not in body
    assert body["server"]["log_level"] == "INFO"
    assert body["metadata"]["boot_only"] == ["host", "port"]


def test_get_settings_503_when_daemon_unreachable(client):
    with patch(
        "llm.providers.omlx.get_settings",
        new=AsyncMock(return_value={"ok": False, "message": "Connection refused"}),
    ):
        r = client.get(f"/api/settings/network-servers/ns-test/settings?token={_TOKEN}")
    assert r.status_code == 503
    assert "Connection refused" in r.json()["detail"]



def test_lifecycle_invalid_action_400s(client):
    r = client.post(
        f"/api/settings/network-servers/ns-test/lifecycle/restart?token={_TOKEN}"
    )
    assert r.status_code == 400
    assert "Invalid lifecycle action" in r.json()["detail"]


def test_lifecycle_unknown_server_404s(client):
    r = client.post(
        f"/api/settings/network-servers/ns-missing/lifecycle/start?token={_TOKEN}"
    )
    assert r.status_code == 404


def test_lifecycle_reload_dispatches_to_discover(client):
    discover_payload = {
        "ok": True, "discovered": 4, "updated": 2, "missing": 0,
    }
    with patch(
        "llm.catalog.discover_network_models",
        new=AsyncMock(return_value=discover_payload),
    ):
        r = client.post(
            f"/api/settings/network-servers/ns-test/lifecycle/reload?token={_TOKEN}"
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["discovered"] == 4
    assert "4" in body["message"]


def test_lifecycle_reload_propagates_failure_message(client):
    with patch(
        "llm.catalog.discover_network_models",
        new=AsyncMock(return_value={
            "ok": False, "discovered": 0, "updated": 0, "missing": 0,
            "error": "Server is offline",
        }),
    ):
        r = client.post(
            f"/api/settings/network-servers/ns-test/lifecycle/reload?token={_TOKEN}"
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "offline" in body["message"]


def test_lifecycle_start_dispatches_to_provider_connector(client, monkeypatch):
    from llm import network_discovery as _nd
    monkeypatch.setattr(_nd, "update_server_status", lambda *a, **k: None)
    with patch(
        "llm.providers.omlx.start_server",
        new=AsyncMock(return_value={
            "ok": True,
            "pid": 12345,
            "host": "127.0.0.1",
            "port": 5200,
            "message": "oMLX started on 127.0.0.1:5200 (pid 12345)",
        }),
    ):
        r = client.post(
            f"/api/settings/network-servers/ns-test/lifecycle/start?token={_TOKEN}"
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["pid"] == 12345


def test_lifecycle_stop_dispatches_to_provider_connector(client, monkeypatch):
    from llm import network_discovery as _nd
    monkeypatch.setattr(_nd, "update_server_status", lambda *a, **k: None)
    with patch(
        "llm.providers.omlx.stop_server",
        new=AsyncMock(return_value={"ok": True, "message": "Stopped"}),
    ):
        r = client.post(
            f"/api/settings/network-servers/ns-test/lifecycle/stop?token={_TOKEN}"
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_lifecycle_unknown_provider_returns_clean_error(client, monkeypatch):
    from llm import network_discovery as _nd
    monkeypatch.setattr(
        _nd,
        "get_server",
        lambda sid: {"id": sid, "provider": "retired", "host": "127.0.0.1", "port": 5100}
        if sid == "ns-retired"
        else None,
    )
    r = client.post(
        f"/api/settings/network-servers/ns-retired/lifecycle/start?token={_TOKEN}"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "retired" in body["message"]


def test_loading_a_model_clears_why_it_was_not_auto_loaded(client, tmp_path, monkeypatch):
    from llm import local_downloads
    from llm.local_model import LocalModel

    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768, "Qwen 3.8 27B")
    local_downloads.write(local_downloads.Download(model, server_id="ns-test"))
    local_downloads.set_detail(model.model, "Not loaded: needs 174 GB of memory, 123 GB free")

    with patch("llm.providers.omlx.load_model", new=AsyncMock(return_value={"ok": True})), \
            patch("llm.network_discovery.discover_and_notify", new=AsyncMock()):
        r = client.post("/api/settings/network-servers/ns-test/models/load", params=_auth(), json={"model": model.model})

    assert r.status_code == 200, r.text
    assert local_downloads.get(model.model).detail is None


def _auth() -> dict[str, str]:
    return dict(zip(("token",), (_TOKEN,)))
