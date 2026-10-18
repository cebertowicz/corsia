from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from types import SimpleNamespace

from fastapi.testclient import TestClient
from spake2 import SPAKE2_Symmetric

import api.devices as devices_api
from core.device_pairing import PAIRING_IDENTITY, _split_key
from core.devices_runtime import runtime
from main import app


def _encoded(value: bytes) -> str:
    return base64.b64encode(value).decode()


def _start(client: TestClient, message: bytes):
    return client.post(
        "/api/devices/pair/start",
        json={"mode": "verified", "host": "studio.local", "msg_c": _encoded(message)},
    )


def test_pairing_events_record_outcomes_without_secret_material(monkeypatch):
    events = []
    monkeypatch.setattr(
        devices_api,
        "emit_product_event",
        lambda category, action, *, properties=None: events.append((category, action, properties)),
    )
    client = TestClient(app)

    runtime.pairing.disable()
    expired = _start(client, b"expired-client")
    assert expired.status_code == 409

    runtime.pairing.enable()
    monkeypatch.setattr(devices_api, "_require_local_management", lambda request: None)
    issued = asyncio.run(
        devices_api.issue_code(
            SimpleNamespace(),
            _user={"id": "person", "token_kind": "session"},
        )
    )
    wrong_code = issued["code"]
    wrong_pairing = SPAKE2_Symmetric(
        hashlib.sha256(PAIRING_IDENTITY + b"\0" + wrong_code.encode()).digest(),
        idSymmetric=PAIRING_IDENTITY,
    )
    wrong_start = _start(client, wrong_pairing.start())
    wrong = client.post(
        "/api/devices/pair/finish",
        json={"exchange_id": wrong_start.json()["exchange_id"], "confirm_c": _encoded(b"wrong-confirmation")},
    )
    assert wrong.status_code == 401

    success_code = runtime.pairing.issue_code()
    success_pairing = SPAKE2_Symmetric(
        hashlib.sha256(PAIRING_IDENTITY + b"\0" + success_code.encode()).digest(),
        idSymmetric=PAIRING_IDENTITY,
    )
    msg_c = success_pairing.start()
    success_start = _start(client, msg_c)
    msg_s = base64.b64decode(success_start.json()["msg_s"])
    client_key, _ = _split_key(success_pairing.finish(msg_s))
    transcript = PAIRING_IDENTITY + b"verified:studio.local\0" + msg_c + msg_s
    success = client.post(
        "/api/devices/pair/finish",
        json={
            "exchange_id": success_start.json()["exchange_id"],
            "confirm_c": _encoded(hmac.new(client_key, transcript, hashlib.sha256).digest()),
        },
    )
    assert success.status_code == 200

    runtime.pairing.enable()
    runtime.pairing._paused = True
    paused = _start(client, b"paused-client")
    assert paused.status_code == 409

    assert {(category, action) for category, action, _ in events} == {
        ("devices", "pairing_code_issued"),
        ("devices", "pairing_failed"),
        ("devices", "pairing_succeeded"),
        ("devices", "pairing_paused"),
    }
    assert {properties["reason"] for _, action, properties in events if action == "pairing_failed"} == {
        "expired",
        "wrong_code",
        "paused",
    }
    rendered = repr(events)
    for secret in (wrong_code, success_code, "wrong-confirmation", "expired-client", "paused-client"):
        assert secret not in rendered
