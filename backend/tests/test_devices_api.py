from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import api.devices as devices_api
import api.system as system_api
from core.device_pairing import DevicePairing
from core.devices_runtime import DevicesRuntime, runtime
from infra import dns_sd
from infra.auth import require_human_user
from infra.devices_listener import DevicesApp, DevicesListener
from infra.settings import set_devices_enabled, set_devices_outside_address
from main import app
from tests.test_dns_sd import _DNSLibrary, _install_dns_seams


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


def test_outside_address_contract_vectors_are_canonical():
    vectors = json.loads((Path(__file__).parents[2] / "testdata" / "outside-address-vectors.json").read_text())
    for vector in vectors["accepted"]:
        assert devices_api._outside_address(vector["input"]) == vector["normalized"]
    for value in vectors["rejected"]:
        with pytest.raises(HTTPException):
            devices_api._outside_address(value)


def test_outside_address_is_saved_by_the_devices_owner_and_returned(monkeypatch):
    writes = []

    async def fake_run_in_db(function, *args):
        writes.append((function, args))

    monkeypatch.setattr(devices_api, "_require_local_management", lambda request: None)
    monkeypatch.setattr(devices_api, "run_in_db", fake_run_in_db)
    monkeypatch.setattr(runtime, "outside_address", None)

    response = asyncio.run(
        devices_api.put_devices(
            {"outside_address": "https://home.example.com:8443"},
            request=object(),
            _user={"id": "person", "token_kind": "session"},
        )
    )

    assert writes == [(set_devices_outside_address, ("home.example.com:8443",))]
    assert response["outside_address"] == "home.example.com:8443"
    with pytest.raises(HTTPException, match="without http://"):
        asyncio.run(
            devices_api.put_devices(
                {"outside_address": "http://home.example.com"},
                request=object(),
                _user={"id": "person", "token_kind": "session"},
            )
        )


@pytest.mark.parametrize(("address", "action"), [("home.example.com", "outside_address_saved"), (None, "outside_address_cleared")])
def test_outside_address_events_never_disclose_the_address(monkeypatch, address, action):
    events = []

    async def write_setting(*_args):
        return None

    monkeypatch.setattr(devices_api, "_require_local_management", lambda _request: None)
    monkeypatch.setattr(devices_api, "run_in_db", write_setting)
    monkeypatch.setattr(devices_api, "emit_product_event", lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(runtime, "outside_address", None)
    asyncio.run(devices_api.put_devices({"outside_address": address}, request=object(), _user={"id": "person"}))
    assert events == [(("devices", action), {"properties": None})]


def test_outside_address_endpoint_returns_set_and_cleared_values():
    client = TestClient(app)
    set_devices_outside_address("home.example.com:8443")
    assert client.get("/api/devices/outside-address").json() == {"outside_address": "home.example.com:8443"}
    set_devices_outside_address(None)
    assert client.get("/api/devices/outside-address").json() == {"outside_address": None}


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


def test_host_key_failure_leaves_runtime_alive_and_devices_closed(monkeypatch):
    import infra.devices_listener as listener_module

    local_runtime = DevicesRuntime()
    monkeypatch.setattr(
        listener_module,
        "ensure_host_key",
        lambda: (_ for _ in ()).throw(RuntimeError("unsafe key")),
    )

    started = asyncio.run(local_runtime.enable(object()))

    assert started is False
    assert local_runtime.enabled is False
    assert local_runtime.snapshot()["listening"] == {"error": "Devices listener credentials unavailable", "reason": "credentials"}
    assert local_runtime.snapshot()["announced_name"] is None


def test_disabled_development_put_refuses_without_binding_or_announcing(monkeypatch):
    local_runtime = DevicesRuntime()

    async def must_not_start(self):
        raise AssertionError("development disable must not bind or announce")

    monkeypatch.setenv("CORSIA_DEVICES_DISABLE", "1")
    monkeypatch.setattr(devices_api, "runtime", local_runtime)
    monkeypatch.setattr(devices_api, "_require_local_management", lambda request: None)
    monkeypatch.setattr(DevicesListener, "start", must_not_start)

    response = asyncio.run(
        devices_api.put_devices(
            {"enabled": True},
            request=SimpleNamespace(app=object()),
            _user={"id": "person", "token_kind": "session"},
        )
    )

    assert response["enabled"] is False
    assert response["listening"] == {"error": "Devices listener disabled", "reason": "disabled"}
    assert response["announced_name"] is None


def test_repeated_enable_preserves_listener_and_announcement_then_disable_tears_down(monkeypatch):
    local_runtime = DevicesRuntime()
    started = []
    stopped = []

    async def fake_start(self):
        started.append(self)
        self.task = object()
        self.port = 47901
        self.announcement = SimpleNamespace(announced_name="CORSIA")
        return True

    async def fake_stop(self):
        stopped.append(self)

    async def fake_run_in_db(*_args):
        return None

    monkeypatch.setattr(devices_api, "runtime", local_runtime)
    monkeypatch.setattr(devices_api, "_require_local_management", lambda request: None)
    monkeypatch.setattr(devices_api, "run_in_db", fake_run_in_db)
    monkeypatch.setattr(DevicesListener, "start", fake_start)
    monkeypatch.setattr(DevicesListener, "stop", fake_stop)
    request = SimpleNamespace(app=object())

    first = asyncio.run(devices_api.put_devices({"enabled": True}, request=request, _user={}))
    first_listener = local_runtime.listener
    first_code = first["code"]["value"]
    repeated = asyncio.run(devices_api.put_devices({"enabled": True}, request=request, _user={}))
    disabled = asyncio.run(devices_api.put_devices({"enabled": False}, request=request, _user={}))

    assert started == [first_listener]
    assert repeated["code"]["value"] == first_code
    assert repeated["announced_name"] == "CORSIA"
    assert stopped == [first_listener]
    assert disabled["enabled"] is False
    assert disabled["announced_name"] is None
    assert disabled["listening"] == {"error": None}


def test_devices_snapshot_gives_a_typeable_host_and_leaves_the_display_name_to_the_resolver(monkeypatch):
    import core.devices_runtime as devices_runtime

    announcements = []
    expected_announcement = SimpleNamespace(announced_name="Announced name")
    devices = DevicesRuntime(
        name_resolver=lambda announcement: announcements.append(announcement) or "Studio Mac"
    )
    devices.listener = SimpleNamespace(
        listening={"port": 47901},
        announcement=expected_announcement,
        port=47901,
    )
    monkeypatch.setattr(devices_runtime, "local_hostname", lambda: "studio-mac.local")
    monkeypatch.setattr(devices_runtime, "computer_name", lambda: "Studio Mac")

    snapshot = devices.snapshot()

    # local_hostname is what the joiner types, so it is the .local host and never the display name (spaces, no DNS).
    assert snapshot["local_hostname"] == "studio-mac.local"
    # computer_name is the person's name for the machine, so the page's fallback never shows the mDNS host.
    assert snapshot["computer_name"] == "Studio Mac"
    assert devices.remote_connect_name() == "Studio Mac"
    assert announcements == [expected_announcement]


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


def test_restore_enabled_devices_at_bootstrapped_state_root_does_not_mint_code(monkeypatch):
    import core.devices_runtime as devices_runtime

    restored = DevicesRuntime()

    async def fake_enable(self, app):
        self.pairing.enable()
        return True

    root = Path("/installed-state")
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv("CORSIA_DEVICES_DISABLE", raising=False)
    monkeypatch.setattr(devices_runtime, "STATE_ROOT", root)
    monkeypatch.setattr(devices_runtime, "_read_prod_state_root", lambda: root)
    monkeypatch.setattr(DevicesRuntime, "enable", fake_enable)

    asyncio.run(restored.restore(object(), enabled=True))

    assert restored.enabled is True
    assert restored.pairing.status()["state"] == "none"


def test_restore_enabled_devices_skips_a_different_state_root(monkeypatch):
    import core.devices_runtime as devices_runtime

    restored = DevicesRuntime()
    enabled = []

    async def fake_enable(self, app):
        enabled.append(app)
        self.pairing.enable()
        return True

    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv("CORSIA_DEVICES_DISABLE", raising=False)
    monkeypatch.setattr(devices_runtime, "STATE_ROOT", Path("/development-state"))
    monkeypatch.setattr(devices_runtime, "_read_prod_state_root", lambda: Path("/installed-state"))
    monkeypatch.setattr(DevicesRuntime, "enable", fake_enable)

    asyncio.run(restored.restore(object(), enabled=True))

    assert enabled == []
    assert restored.enabled is False


@pytest.mark.parametrize("veto", ["CORSIA_DEVICES_DISABLE", "CORSIA_TEST_MODE"])
def test_restore_veto_wins_even_at_the_bootstrapped_state_root(monkeypatch, veto):
    import core.devices_runtime as devices_runtime

    restored = DevicesRuntime()
    enabled = []
    root = Path("/installed-state")

    async def fake_enable(self, app):
        enabled.append(app)
        return True

    monkeypatch.setenv(veto, "1")
    monkeypatch.setattr(devices_runtime, "STATE_ROOT", root)
    monkeypatch.setattr(devices_runtime, "_read_prod_state_root", lambda: root)
    monkeypatch.setattr(DevicesRuntime, "enable", fake_enable)

    asyncio.run(restored.restore(object(), enabled=True))

    assert enabled == []


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


def test_the_desktop_shells_own_requests_pass_the_real_gate_in_the_format_it_sends(monkeypatch):
    """The shell's version check and pairing calls carry no token (reqwest, JSON body); the gate must not need one."""
    runtime.pairing.disable()
    shell_headers = {"user-agent": "corsia-desktop", "accept": "*/*"}
    lan = TestClient(DevicesApp(app), client=("192.168.1.20", 50000))
    beyond = TestClient(DevicesApp(app), client=("8.8.8.8", 50000))

    version = lan.get("/api/version", headers=shell_headers)
    start = lan.post("/api/devices/pair/start", json={"mode": "pinned", "msg_c": "Y2xpZW50"}, headers=shell_headers)
    finish = lan.post(
        "/api/devices/pair/finish", json={"exchange_id": "none", "confirm_c": "Y29uZmlybQ=="}, headers=shell_headers,
    )
    refused = beyond.get("/api/version", headers=shell_headers)

    assert version.status_code == 200
    assert start.status_code == 409
    assert finish.status_code not in {401, 403}
    assert refused.status_code == 403


def test_active_code_is_redacted_outside_direct_main_loopback(monkeypatch):
    async def human_session(_scope):
        return True

    runtime.pairing.disable()
    runtime.pairing.enable()
    code = runtime.pairing.issue_code()
    monkeypatch.setitem(
        app.dependency_overrides,
        devices_api.require_human_user,
        lambda: {"id": "person", "token_kind": "session"},
    )
    monkeypatch.setattr(
        DevicesApp,
        "_has_human_session",
        staticmethod(human_session),
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
        "expires_in_seconds": forwarded.json()["code"]["expires_in_seconds"],
    }
    assert devices_listener.json()["code"] == {
        "state": "active",
        "expires_in_seconds": devices_listener.json()["code"]["expires_in_seconds"],
    }


_DOCUMENTED_DEVICES_KEYS = {"enabled", "listening", "announced_name", "local_hostname", "computer_name", "lan_addresses", "code", "outside_address"}
_NAME_CONFLICT = -65548


def _register_conflict():
    return _DNSLibrary(register_results=[_NAME_CONFLICT]), lambda library, announcement: None


def _callback_conflict():
    def report(library, _announcement):
        library.callback(None, 0, _NAME_CONFLICT, None, None, None, None)

    return _DNSLibrary(register_results=[dns_sd._KDNS_SERVICE_ERR_NO_ERROR]), report


def _daemon_lost():
    def lose(library, announcement):
        announcement._process(library)
        announcement._process(library)

    library = _DNSLibrary(
        register_results=[dns_sd._KDNS_SERVICE_ERR_NO_ERROR, dns_sd._KDNS_SERVICE_ERR_NO_ERROR],
        process_results=[dns_sd._KDNS_SERVICE_ERR_SERVICE_NOT_RUNNING] * 2,
    )
    return library, lose


def _response_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _response_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _response_keys(item)


@pytest.mark.parametrize(
    "failure",
    [_register_conflict, _callback_conflict, _daemon_lost],
    ids=["register-conflict", "callback-conflict", "daemon-lost"],
)
def test_devices_status_omits_dns_sd_failure_cause(monkeypatch, failure):
    """G12: a DNS-SD failure reaches GET /api/devices only as no announced name."""
    import core.devices_runtime as devices_runtime

    library, fail = failure()
    with pytest.MonkeyPatch.context() as dns_seams:
        _install_dns_seams(dns_seams, library)
        announcement = dns_sd.DNSServiceAnnouncement(47901)
        announcement.start()
        fail(library, announcement)
    raw_error = announcement.error
    assert raw_error is not None and "DNS-SD" in raw_error
    listener = DevicesListener(app, port=47901, announcement=announcement)
    runtime.pairing.disable()
    runtime.pairing.enable()
    monkeypatch.setattr(runtime, "listener", listener)
    monkeypatch.setattr(runtime, "_name_resolver", lambda _announcement: "studio.local")
    monkeypatch.setattr(devices_runtime, "lan_addresses", lambda *, include_link_local: ["192.168.1.20"])
    monkeypatch.setitem(
        app.dependency_overrides,
        devices_api.require_human_user,
        lambda: {"id": "person", "token_kind": "session"},
    )

    response = TestClient(app, client=("127.0.0.1", 1234)).get("/api/devices")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == _DOCUMENTED_DEVICES_KEYS
    assert body["listening"] == {"port": 47901}
    assert body["announced_name"] is None
    assert body["code"] == {"state": "none"}
    assert "expires_in_seconds" not in body["code"]
    assert not {"cause", "error", "reason", "detail", "message"} & set(_response_keys(body))
    assert raw_error not in response.text
    assert "DNS-SD" not in response.text
    assert str(_NAME_CONFLICT) not in response.text
    assert str(dns_sd._KDNS_SERVICE_ERR_SERVICE_NOT_RUNNING) not in response.text


def test_devices_status_when_the_registration_callback_never_arrives(monkeypatch):
    """A Mac whose network never lets mDNSResponder confirm the name: no hang, no error, a state to act on."""
    import core.devices_runtime as devices_runtime

    library = _DNSLibrary(register_results=[dns_sd._KDNS_SERVICE_ERR_NO_ERROR])
    with pytest.MonkeyPatch.context() as dns_seams:
        _install_dns_seams(dns_seams, library)
        announcement = dns_sd.DNSServiceAnnouncement(47901)
        assert announcement.start() is True
    assert announcement.announced_name is None and announcement.error is None
    listener = DevicesListener(app, port=47901, announcement=announcement)
    runtime.pairing.disable()
    runtime.pairing.enable()
    monkeypatch.setattr(runtime, "listener", listener)
    monkeypatch.setattr(devices_runtime, "lan_addresses", lambda *, include_link_local: ["192.168.1.20"])
    monkeypatch.setitem(
        app.dependency_overrides,
        devices_api.require_human_user,
        lambda: {"id": "person", "token_kind": "session"},
    )

    response = TestClient(app, client=("127.0.0.1", 1234)).get("/api/devices")

    assert response.status_code == 200
    body = response.json()
    assert body["listening"] == {"port": 47901}
    assert body["announced_name"] is None
    assert body["lan_addresses"] == ["192.168.1.20"]
    assert body["local_hostname"]


def test_devices_status_reflects_the_name_announced_after_the_snapshot(monkeypatch):
    """The callback lands after PUT returned: the change schedules devices_updated and GET shows the name."""
    import core.devices_runtime as devices_runtime

    library = _DNSLibrary(register_results=[dns_sd._KDNS_SERVICE_ERR_NO_ERROR])
    updates: list[str] = []
    with pytest.MonkeyPatch.context() as dns_seams:
        _install_dns_seams(dns_seams, library)
        announcement = dns_sd.DNSServiceAnnouncement(47901)
        announcement.on_change = lambda: updates.append("devices_updated")
        assert announcement.start() is True
    listener = DevicesListener(app, port=47901, announcement=announcement)
    runtime.pairing.disable()
    runtime.pairing.enable()
    monkeypatch.setattr(runtime, "listener", listener)
    monkeypatch.setattr(devices_runtime, "lan_addresses", lambda *, include_link_local: ["192.168.1.20"])
    monkeypatch.setitem(
        app.dependency_overrides,
        devices_api.require_human_user,
        lambda: {"id": "person", "token_kind": "session"},
    )
    client = TestClient(app, client=("127.0.0.1", 1234))

    assert client.get("/api/devices").json()["announced_name"] is None
    library.callback(None, 0, 0, b"Studio", None, None, None)
    library.callback(None, 0, 0, b"Studio", None, None, None)

    assert updates == ["devices_updated"]
    assert client.get("/api/devices").json()["announced_name"] == "Studio"


def test_devices_management_routes_require_direct_main_loopback(monkeypatch):
    async def human_session(_scope):
        return True

    writes = []

    async def fake_enable(self, app):
        self.pairing.enable()
        return True

    async def fake_run_in_db(function, *args):
        writes.append((function, args))

    runtime.pairing.disable()
    updates: list[dict[str, str]] = []
    monkeypatch.setattr(runtime.pairing, "_on_update", lambda: updates.append({"type": "devices_updated"}))
    monkeypatch.setattr(DevicesRuntime, "enable", fake_enable)
    monkeypatch.setattr(devices_api, "run_in_db", fake_run_in_db)
    monkeypatch.setattr(runtime, "_name_resolver", lambda _announcement: "Studio")
    monkeypatch.setitem(
        app.dependency_overrides,
        devices_api.require_human_user,
        lambda: {"id": "person", "token_kind": "session"},
    )
    monkeypatch.setattr(
        DevicesApp,
        "_has_human_session",
        staticmethod(human_session),
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
    assert isinstance(direct_code.json()["code"], str)
    assert len(direct_code.json()["code"]) == 4
    assert updates == [{"type": "devices_updated"}, {"type": "devices_updated"}]
    assert writes == [(set_devices_enabled, (True,))]
    for refused in (forwarded_enable, forwarded_code, devices_enable, devices_code):
        assert refused.status_code == 403
        assert refused.json()["detail"] == "Manage connections on Studio itself"
