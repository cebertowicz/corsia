from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import api.devices as devices_api
import api.system as system_api
from core.device_pairing import DevicePairing
from core.devices_runtime import DevicesRuntime, runtime
from infra.auth import require_human_user
from infra.devices_listener import DevicesApp
from infra.settings import set_devices_enabled
from main import app


def test_devices_switch_has_one_person_only_writer(monkeypatch):
    writes = []

    async def fake_disable(self):
        return None

    async def fake_run_in_db(function, *args):
        writes.append((function, args))

    monkeypatch.setattr(runtime, "pairing", DevicePairing())
    runtime.pairing.disable()
    monkeypatch.setattr(devices_api, "_require_local_management", lambda request: None)
    monkeypatch.setattr(DevicesRuntime, "disable", fake_disable)
    monkeypatch.setattr(devices_api, "run_in_db", fake_run_in_db)

    response = asyncio.run(
        devices_api.put_devices(
            {"enabled": False},
            request=object(),
            _user={"id": "person", "token_kind": "session"},
        )
    )

    assert response["enabled"] is False
    assert writes == [(set_devices_enabled, (False,))]
    with pytest.raises(HTTPException, match="Devices must be changed") as settings_error:
        asyncio.run(
            system_api.update_settings(
                {"devices": {"enabled": True}},
                user={"id": "person", "token_kind": "session"},
            )
        )
    assert settings_error.value.status_code == 400
    with pytest.raises(HTTPException) as agent_error:
        require_human_user({"id": "agent", "token_kind": "agent"})
    assert agent_error.value.status_code == 403
    assert runtime.pairing.status()["state"] == "none"


def test_direct_loopback_enable_mints_first_code_only_after_listener_starts(monkeypatch):
    writes = []

    async def fake_enable(self, app):
        self.pairing.enable()
        return True

    async def fake_run_in_db(function, *args):
        writes.append((function, args))

    runtime.pairing.disable()
    monkeypatch.setattr(devices_api, "_require_local_management", lambda request: None)
    monkeypatch.setattr(DevicesRuntime, "enable", fake_enable)
    monkeypatch.setattr(devices_api, "run_in_db", fake_run_in_db)

    response = asyncio.run(
        devices_api.put_devices(
            {"enabled": True},
            request=SimpleNamespace(app=object()),
            _user={"id": "person", "token_kind": "session"},
        )
    )

    assert response["enabled"] is True
    assert response["code"]["state"] == "active"
    assert len(response["code"]["value"]) == 4
    assert writes == [(set_devices_enabled, (True,))]


def test_failed_listener_start_does_not_mint_code(monkeypatch):
    async def fake_enable(self, app):
        return False

    runtime.pairing.disable()
    monkeypatch.setattr(devices_api, "_require_local_management", lambda request: None)
    monkeypatch.setattr(DevicesRuntime, "enable", fake_enable)

    response = asyncio.run(
        devices_api.put_devices(
            {"enabled": True},
            request=SimpleNamespace(app=object()),
            _user={"id": "person", "token_kind": "session"},
        )
    )

    assert response["enabled"] is False
    assert response["code"]["state"] == "none"


@pytest.mark.parametrize(
    "connection_scope",
    [
        {"type": "http", "headers": [], "client": ("192.168.1.5", 1234)},
        {
            "type": "http",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "corsia_listener": "devices",
        },
    ],
)
def test_non_local_or_devices_listener_enable_mints_no_code(connection_scope):
    from starlette.requests import Request

    runtime.pairing.disable()
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            devices_api.put_devices(
                {"enabled": True},
                request=Request(connection_scope),
                _user={"id": "person", "token_kind": "session"},
            )
        )

    assert error.value.status_code == 403
    assert runtime.pairing.status()["state"] == "none"


def test_restore_enabled_devices_does_not_mint_code(monkeypatch):
    restored = DevicesRuntime()

    async def fake_enable(self, app):
        self.pairing.enable()
        return True

    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv("CORSIA_STATE_DIR", raising=False)
    monkeypatch.delenv("CORSIA_DEVICES_DISABLE", raising=False)
    monkeypatch.setattr(DevicesRuntime, "enable", fake_enable)

    asyncio.run(restored.restore(object(), enabled=True))

    assert restored.enabled is True
    assert restored.pairing.status()["state"] == "none"


def test_other_settings_write_mints_no_code():
    runtime.pairing.disable()
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            system_api.update_settings(
                {"devices": {"enabled": True}},
                user={"id": "person", "token_kind": "session"},
            )
        )

    assert error.value.status_code == 400
    assert runtime.pairing.status()["state"] == "none"


def test_devices_address_hints_are_authenticated_and_not_pre_auth(monkeypatch):
    import core.devices_runtime as devices_runtime

    runtime.pairing.disable()
    monkeypatch.setattr(devices_runtime, "local_hostname", lambda: "studio.local")
    monkeypatch.setattr(
        devices_runtime,
        "lan_addresses",
        lambda *, include_link_local: ["192.168.1.20", "fd00::20"],
    )
    monkeypatch.setitem(
        app.dependency_overrides,
        devices_api.require_human_user,
        lambda: {"id": "person", "token_kind": "session"},
    )
    client = TestClient(app)

    authenticated = client.get("/api/devices")

    assert authenticated.status_code == 200
    assert authenticated.json()["local_hostname"] == "studio.local"
    assert authenticated.json()["lan_addresses"] == ["192.168.1.20", "fd00::20"]
    monkeypatch.delitem(app.dependency_overrides, devices_api.require_human_user, raising=False)
    unauthenticated = client.get("/api/devices")
    pairing = client.post("/api/devices/pair/start", json={"mode": "pinned", "msg_c": "Y2xpZW50"})
    version = client.get("/api/version")

    assert unauthenticated.status_code == 401
    assert "local_hostname" not in unauthenticated.json()
    assert pairing.status_code == 409
    assert "local_hostname" not in pairing.json()
    assert "lan_addresses" not in pairing.json()
    assert version.status_code == 200
    assert "local_hostname" not in version.json()
    assert "lan_addresses" not in version.json()


def test_active_code_is_redacted_outside_direct_main_loopback(monkeypatch):
    runtime.pairing.disable()
    runtime.pairing.enable()
    code = runtime.pairing.issue_code()
    monkeypatch.setitem(
        app.dependency_overrides,
        devices_api.require_human_user,
        lambda: {"id": "person", "token_kind": "session"},
    )
    main_client = TestClient(app, client=("127.0.0.1", 1234))
    devices_client = TestClient(DevicesApp(app), client=("127.0.0.1", 1234))

    direct = main_client.get("/api/devices")
    forwarded = main_client.get("/api/devices", headers={"x-forwarded-for": "192.168.1.20"})
    devices_listener = devices_client.get("/api/devices")

    assert direct.status_code == forwarded.status_code == devices_listener.status_code == 200
    assert direct.json()["code"]["value"] == code
    assert forwarded.json()["code"] == {
        "state": "active",
        "expires_at": forwarded.json()["code"]["expires_at"],
    }
    assert devices_listener.json()["code"] == {
        "state": "active",
        "expires_at": devices_listener.json()["code"]["expires_at"],
    }


def test_devices_management_routes_require_direct_main_loopback(monkeypatch):
    writes = []

    async def fake_enable(self, app):
        self.pairing.enable()
        return True

    async def fake_run_in_db(function, *args):
        writes.append((function, args))

    runtime.pairing.disable()
    monkeypatch.setattr(DevicesRuntime, "enable", fake_enable)
    monkeypatch.setattr(devices_api, "run_in_db", fake_run_in_db)
    monkeypatch.setattr(devices_api, "_management_name", lambda: "Studio")
    monkeypatch.setitem(
        app.dependency_overrides,
        devices_api.require_human_user,
        lambda: {"id": "person", "token_kind": "session"},
    )
    main_client = TestClient(app, client=("127.0.0.1", 1234))
    devices_client = TestClient(DevicesApp(app), client=("127.0.0.1", 1234))

    direct_enable = main_client.put("/api/devices", json={"enabled": True})
    direct_code = main_client.post("/api/devices/code")
    forwarded_enable = main_client.put(
        "/api/devices", json={"enabled": True}, headers={"x-forwarded-for": "192.168.1.20"}
    )
    forwarded_code = main_client.post("/api/devices/code", headers={"x-forwarded-for": "192.168.1.20"})
    devices_enable = devices_client.put("/api/devices", json={"enabled": True})
    devices_code = devices_client.post("/api/devices/code")

    assert direct_enable.status_code == direct_code.status_code == 200
    assert direct_enable.json()["code"]["state"] == "active"
    assert direct_code.json()["code"]
    assert writes == [(set_devices_enabled, (True,))]
    for refused in (forwarded_enable, forwarded_code, devices_enable, devices_code):
        assert refused.status_code == 403
        assert refused.json()["detail"] == "Manage connections on Studio itself"
