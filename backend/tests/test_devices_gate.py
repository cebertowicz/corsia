"""The Devices listener's gate: who may reach it, and which token it and the API routes agree on."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import infra.auth
from infra import devices_listener
from infra.auth import require_human_user
from infra.devices_listener import DevicesApp

_LAN_PEER = ("192.168.1.20", 50000)
_SESSIONS = {
    "person-session": {"id": "person", "name": "Person", "token_kind": "session"},
    "other-person-session": {"id": "other", "name": "Other", "token_kind": "session"},
    "agent-token": {"id": "agent", "name": "Agent", "token_kind": "agent"},
}


@pytest.fixture
def sessions(monkeypatch):
    """One credential table behind both the gate and the real `require_human_user` the routes use."""

    async def fake_run_in_db(function, *args):
        return _SESSIONS.get(args[0])

    monkeypatch.setattr(devices_listener, "run_in_db", fake_run_in_db)
    monkeypatch.setattr(infra.auth, "authenticate", _SESSIONS.get)


def _app() -> FastAPI:
    stub = FastAPI()

    @stub.get("/api/devices")
    async def devices(user: dict = Depends(require_human_user)):
        return {"user": user["id"]}

    @stub.get("/api/health")
    async def health():
        return {"ok": True}

    @stub.websocket("/ws")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"type": "accepted"})
        await websocket.close()

    return stub


def _client(peer=_LAN_PEER) -> TestClient:
    return TestClient(DevicesApp(_app()), client=peer)


@pytest.mark.parametrize(
    "peer",
    ["192.168.1.20", "10.1.2.3", "172.16.5.5", "169.254.9.9", "fe80::1%en0", "fd00::5", "127.0.0.1", "::1", "::ffff:192.168.1.5"],
)
def test_peers_on_this_network_reach_the_listener(sessions, peer):
    assert _client((peer, 50000)).get("/api/health").status_code == 200


@pytest.mark.parametrize("peer", ["8.8.8.8", "2001:db8::1", "2a00:1450::1", "100.64.1.2", "::ffff:8.8.8.8", "testclient"])
def test_peers_beyond_this_network_get_nothing_even_on_public_routes(sessions, peer):
    client = _client((peer, 50000))

    refused = client.get("/api/health")

    assert refused.status_code == 403
    assert refused.json() == {"reason": "not_on_this_network"}
    with pytest.raises(WebSocketDisconnect) as socket:
        with client.websocket_connect("/ws"):
            pass
    assert socket.value.code == 1008


def test_a_scope_with_no_peer_address_is_refused():
    assert DevicesApp._peer_is_on_this_network({}) is False
    assert DevicesApp._peer_is_on_this_network({"client": None}) is False
    assert DevicesApp._peer_is_on_this_network({"client": ("", 0)}) is False


@pytest.mark.parametrize(
    "query",
    [
        "token=person-session&token=agent-token",
        "token=agent-token&token=person-session",
        "token=person-session&token=person-session",
        "token=person-session&token=",
        "token=&token=person-session",
    ],
)
def test_a_request_with_two_query_tokens_is_refused_whichever_one_the_route_would_read(sessions, query):
    refused = _client().get(f"/api/devices?{query}")

    assert refused.status_code == 401
    assert refused.json() == {"reason": "sign_in_required"}


def test_a_request_with_two_authorization_headers_is_refused(sessions):
    headers = [("authorization", "Bearer person-session"), ("authorization", "Bearer agent-token")]

    # A TestClient merges repeated headers, so send the pair as the raw ASGI scope the listener sees.
    scope = {"headers": [(name.encode(), value.encode()) for name, value in headers], "query_string": b""}
    token, ambiguous = DevicesApp._handshake_token(scope)

    assert ambiguous is True
    assert token == "person-session"


def test_a_query_token_and_a_different_authorization_token_are_refused_but_the_same_one_is_not(sessions):
    client = _client()

    differing = client.get(
        "/api/devices?token=person-session", headers={"Authorization": "Bearer agent-token"},
    )
    same = client.get(
        "/api/devices?token=person-session", headers={"Authorization": "Bearer person-session"},
    )

    assert differing.status_code == 401
    assert same.status_code == 200
    assert same.json() == {"user": "person"}


@pytest.mark.asyncio
async def test_the_gate_refuses_an_ambiguous_request_before_authenticating_anything(monkeypatch):
    calls = []

    async def fake_run_in_db(function, *args):
        calls.append(args)
        return {"token_kind": "session"}

    monkeypatch.setattr(devices_listener, "run_in_db", fake_run_in_db)

    assert await DevicesApp._has_human_session({"query_string": b"token=a&token=b"}) is False
    assert await DevicesApp._has_human_session(
        {"headers": [(b"authorization", b"Bearer header-token")], "query_string": b"token=query-token"},
    ) is False
    assert calls == []


def test_a_websocket_handshake_with_two_tokens_is_closed_before_accept(sessions):
    client = _client()

    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/ws?token=person-session&token=agent-token"):
            pass

    assert refused.value.code == 1008
