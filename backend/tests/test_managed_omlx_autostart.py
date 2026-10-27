"""CORSIA's own local model starts again at startup unless the person stopped it in Settings."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth_fixtures import seed_account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm import lifecycle
from llm import managed_omlx_binary as binary

MANAGED = {"id": "own", "provider": "omlx", "kind": "managed", "source": "corsia",
           "instance_id": binary.MANAGED_INSTANCE_ID, "status": "offline"}
PERSONS = {"id": "mine", "provider": "omlx", "kind": "local", "source": "manual",
           "instance_id": "mine", "status": "offline"}
TOKEN = "test-token-02"


def _targets(monkeypatch, servers, providers=None):
    monkeypatch.setattr("llm.network_discovery.get_servers", lambda *a, **k: servers)
    monkeypatch.setattr(lifecycle, "get_providers", lambda: providers or {})
    return lifecycle._autostart_targets()


def test_the_managed_server_starts_when_the_person_never_chose(monkeypatch):
    assert _targets(monkeypatch, [MANAGED]) == [MANAGED]


def test_a_persons_own_server_is_never_started(monkeypatch):
    assert _targets(monkeypatch, [PERSONS], {"omlx": {"autoStart": True}}) == []


def test_a_stopped_or_disabled_managed_server_stays_off(monkeypatch):
    assert _targets(monkeypatch, [MANAGED], {"omlx": {"autoStart": False}}) == []
    assert _targets(monkeypatch, [{**MANAGED, "status": "disabled"}]) == []


@pytest.fixture
def engine(monkeypatch):
    ensure = AsyncMock(return_value={"ok": True, "pid": 7})
    monkeypatch.setattr(lifecycle, "get_managed_engine", lambda _name: SimpleNamespace(ensure_live=ensure))
    monkeypatch.setattr(lifecycle, "_autostart_targets", lambda: [MANAGED])
    monkeypatch.setattr("llm.local_setup_operation.read", lambda: None)
    discover = AsyncMock()
    monkeypatch.setattr("llm.network_discovery._discover_and_notify", discover)
    return ensure, discover


def test_startup_starts_the_server_and_finds_its_models(engine):
    ensure, discover = engine
    asyncio.run(lifecycle.autostart_local_providers())
    ensure.assert_awaited_once_with({"server_id": "own"})
    discover.assert_awaited_once_with("own")


def test_a_failed_start_discovers_nothing_and_does_not_raise(engine):
    ensure, discover = engine
    ensure.return_value = {"ok": False, "message": "Port in use"}
    asyncio.run(lifecycle.autostart_local_providers())
    discover.assert_not_awaited()


def test_a_setup_in_flight_starts_its_own_server(engine, monkeypatch):
    ensure, _discover = engine
    monkeypatch.setattr("llm.local_setup_operation.read", lambda: SimpleNamespace(in_flight=True))
    asyncio.run(lifecycle.autostart_local_providers())
    ensure.assert_not_awaited()


@pytest.fixture
def client(monkeypatch):
    seed_account("u-test", TOKEN)
    rows = {"own": MANAGED, "mine": PERSONS}
    monkeypatch.setattr("llm.network_discovery.get_server", rows.get)
    monkeypatch.setattr("llm.network_discovery.update_server_status", lambda *a, **k: None)
    from api.network_servers import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("server_id,action,saved", [
    ("own", "stop", {"omlx": {"autoStart": False}}),
    ("own", "start", {"omlx": {"autoStart": True}}),
    ("mine", "stop", None),
    ("mine", "start", None),
])
def test_start_and_stop_in_settings_set_whether_startup_runs_the_managed_server(client, server_id, action, saved):
    with (patch(f"llm.providers.omlx.{action}_server", new=AsyncMock(return_value={"ok": True})),
          patch("infra.settings.save_setting_merge") as save):
        response = client.post(f"/api/settings/network-servers/{server_id}/lifecycle/{action}?token={TOKEN}")
    assert response.json()["ok"] is True
    if saved is None:
        save.assert_not_called()
    else:
        save.assert_called_once_with("providers", saved)


def test_a_failed_stop_leaves_startup_as_it_was(client):
    with (patch("llm.providers.omlx.stop_server", new=AsyncMock(return_value={"ok": False, "message": "Still running"})),
          patch("infra.settings.save_setting_merge") as save):
        client.post(f"/api/settings/network-servers/own/lifecycle/stop?token={TOKEN}")
    save.assert_not_called()
