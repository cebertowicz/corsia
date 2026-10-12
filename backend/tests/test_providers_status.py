
from __future__ import annotations

import os
import sys
from unittest.mock import ANY, AsyncMock, patch

import pytest

from auth_fixtures import seed_account
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TOKEN = "test-token-01"


@pytest.fixture
def client(monkeypatch):
    seed_account("u-test", _TOKEN)
    from api.providers import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)



def test_provider_status_rejects_cloud(client):
    r = client.get(f"/api/settings/providers/anthropic/status?token={_TOKEN}")
    assert r.status_code == 400


def test_provider_status_rejects_unknown_provider(client):
    r = client.get(f"/api/settings/providers/junkyzz/status?token={_TOKEN}")
    assert r.status_code == 400



def test_provider_status_omlx_returns_aggregated_servers(client):
    rows = [
        {
            "id": "ns-1", "instance_name": "Studio", "host": "10.0.0.5",
            "port": 5200, "status": "online", "provider": "omlx",
        },
        {
            "id": "ns-2", "instance_name": "Mini", "host": "10.0.0.6",
            "port": 5200, "status": "offline", "provider": "omlx",
        },
    ]
    with (
        patch("llm.network_discovery.get_servers", return_value=rows),
        patch("llm.catalog.list_models", return_value=[]),
    ):
        r = client.get(f"/api/settings/providers/omlx/status?token={_TOKEN}")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "omlx"
    assert body["server"]["running"] is True  # at least one online
    assert len(body["server"]["servers"]) == 2
    assert body["loaded_models"] == []
    assert body["disk_models"] == []


def test_provider_status_mlxd_is_removed(client):
    r = client.get(f"/api/settings/providers/mlxd/status?token={_TOKEN}")
    assert r.status_code == 400
    assert "Unknown provider" in r.text


def test_omlx_update_preserves_managed_autostart_contract(client):
    with (
        patch("api.providers.settings.save_setting_merge") as save,
        patch("api.providers.notify_models_changed"),
    ):
        response = client.put(
            f"/api/settings/providers/omlx?token={_TOKEN}",
            json={"auto_start": True},
        )

    assert response.status_code == 200
    save.assert_called_once_with("providers", {"omlx": {"autoStart": True}}, db=ANY)


def test_provider_status_omlx_no_servers_registered(client):
    with (
        patch("llm.network_discovery.get_servers", return_value=[]),
        patch("llm.catalog.list_models", return_value=[]),
    ):
        r = client.get(f"/api/settings/providers/omlx/status?token={_TOKEN}")
    assert r.status_code == 200
    body = r.json()
    assert body["server"]["running"] is False
    assert body["server"]["servers"] == []
