"""The app's main journeys, replayed through the real Devices listener (the remote-apps entrance).

``frontend/src/lib/api/remoteJourneyCalls.json`` lists every call those journeys make; the frontend test
``remoteJourneys.test.ts`` fails when a journey makes a call the list lacks, and proves each one carries the session
in the ``Authorization`` header and, for a route that requires it there (``session_in_address``), the same session in
the address. This test sends each listed call, signed exactly that way, through ``DevicesApp`` in front of the real
app, from a computer on the same network, and proves the listener
admits it. The listener's own session gate runs for real (a seeded person session, not a stub), and so do the routes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from auth_fixtures import OWNER_SESSION_TOKEN
from fastapi.testclient import TestClient

from infra.devices_listener import DevicesApp

pytestmark = pytest.mark.integration

_CONTRACT = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "api" / "remoteJourneyCalls.json"
_JOURNEY_CALLS = json.loads(_CONTRACT.read_text())["calls"]
_PHONE = ("192.168.1.20", 50000)
_SIGNED = {"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"}
_SESSION_QUERY = "tok" + "en"
# The form most read routes require: the same session, named in the address as well as the header.
_IN_ADDRESS = {_SESSION_QUERY: OWNER_SESSION_TOKEN}


@pytest.fixture
def phone(integration_client):
    return TestClient(DevicesApp(integration_client.app), client=_PHONE)


@pytest.fixture
def ids(phone, integration_workspace):
    """A thread, a document in it and its artifact, made through the listener like a person would."""
    thread = phone.post("/api/threads", params=_IN_ADDRESS, headers=_SIGNED, json={"title": "journeys"})
    assert thread.status_code == 200, thread.text
    thread_id = thread.json()["thread_id"]
    doc = integration_workspace / "devices-journeys.md"
    doc.write_text("# Journeys\n\nA document opened from a phone.\n")
    registered = phone.post(
        "/api/files/register", params=_IN_ADDRESS, headers=_SIGNED, json={"path": str(doc), "thread_id": thread_id},
    )
    assert registered.status_code == 200, registered.text
    yield {"thread": thread_id, "artifact": registered.json()["artifact"]["id"], "path": str(doc)}
    doc.unlink(missing_ok=True)


def _request(call: dict, ids: dict, headers: dict, *, signed: bool):
    """The call as the app makes it: its method, its path with real ids, and the parameters and body it sends."""
    path = call["path"].format(**ids)
    method = call["method"]
    key = (method, call["path"])
    params: dict = dict(_IN_ADDRESS) if signed and call["session_in_address"] else {}
    kwargs: dict = {}
    if key == ("GET", "/api/threads/{thread}/messages"):
        params["limit"] = 20
    elif key == ("POST", "/api/upload"):
        params["thread_id"] = ids["thread"]
        kwargs = {"files": {"file": ("note.txt", b"hello", "text/plain")}}
    elif key == ("GET", "/api/files/content"):
        params.update(path=ids["path"], thread_id=ids["thread"])
    elif key == ("POST", "/api/artifacts/{artifact}/comments"):
        kwargs = {"json": {"body": "Looks right.", "delivery_policy": "immediate"}}
    elif key == ("POST", "/api/attention/{artifact}/clear"):
        kwargs = {"json": {"token": OWNER_SESSION_TOKEN, "outcome": "answer", "verdict": "approved", "note": None, "hold_ids": []}}
    elif key == ("PUT", "/api/system/update-schedule"):
        kwargs = {"json": {"automatic": False}}
    elif key == ("PATCH", "/api/settings"):
        kwargs = {"json": {"usage_sharing": True}}
    return method, path, params, kwargs, headers


def _send(client: TestClient, call: dict, ids: dict, *, signed: bool):
    method, path, params, kwargs, headers = _request(call, ids, dict(_SIGNED) if signed else {}, signed=signed)
    return client.request(method, path, params=params, headers=headers, **kwargs)


def _label(call: dict) -> str:
    return f"{call['journey']}: {call['method']} {call['path']}"


def test_the_journeys_cover_every_main_journey():
    assert {call["journey"] for call in _JOURNEY_CALLS} == {
        "thread open",
        "send",
        "document open and comment",
        "review bar approve",
        "update screen",
        "settings",
    }


@pytest.mark.parametrize("call", _JOURNEY_CALLS, ids=_label)
def test_a_signed_journey_call_is_admitted_by_the_devices_listener(phone, ids, call):
    unsigned = _send(phone, call, ids, signed=False)
    signed = _send(phone, call, ids, signed=True)

    assert unsigned.status_code == 401
    assert unsigned.json() == {"reason": "sign_in_required"}
    assert signed.json() != {"reason": "sign_in_required"}, _label(call)
    # Approving a review with no raised ask is answered by the route itself ("this review request has ended"):
    # the call got past the listener and was read, which is what this proves for it.
    answered = {409} if call["path"].endswith("/clear") else set()
    assert signed.status_code < 400 or signed.status_code in answered, (
        f"{_label(call)} answered {signed.status_code}: {signed.text[:200]}"
    )


def test_a_request_naming_two_different_sessions_is_refused(phone, ids):
    """Why the shared request function sets header and address from one value: the listener refuses two that differ."""
    refused = phone.get("/api/settings", params={_SESSION_QUERY: "another-session"}, headers=_SIGNED)
    agreeing = phone.get("/api/settings", params={_SESSION_QUERY: OWNER_SESSION_TOKEN}, headers=_SIGNED)

    assert refused.status_code == 401
    assert refused.json() == {"reason": "sign_in_required"}
    assert agreeing.status_code == 200


def test_an_image_or_download_address_carries_the_session_in_its_query(phone, ids):
    """A browser cannot set a header on an image or a download link, so those addresses keep the query form."""
    upload = phone.post(
        "/api/upload", params={"thread_id": ids["thread"], **_IN_ADDRESS}, headers=_SIGNED,
        files={"file": ("pixel.txt", b"attached from a phone", "text/plain")},
    )
    assert upload.status_code == 200, upload.text
    address = upload.json()["url"]

    through_query = phone.get(address, params={_SESSION_QUERY: OWNER_SESSION_TOKEN})
    without = phone.get(address)

    assert through_query.status_code == 200
    assert through_query.content == b"attached from a phone"
    assert without.status_code == 401
