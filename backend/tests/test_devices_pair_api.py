from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import api.devices as devices_api
from fastapi.testclient import TestClient
from spake2 import SPAKE2_Symmetric

import api.devices as devices_api
from core.device_pairing import PAIRING_IDENTITY, _split_key
from core.devices_runtime import runtime
from infra.devices_listener import _key_paths
from main import app


def _encoded(value: bytes) -> str:
    return base64.b64encode(value).decode()


def _client_pairing(code: str) -> SPAKE2_Symmetric:
    secret = hashlib.sha256(PAIRING_IDENTITY + b"\0" + code.encode()).digest()
    return SPAKE2_Symmetric(secret, idSymmetric=PAIRING_IDENTITY)


def test_python_spake2_accepts_rust_04_public_vector():
    """Rust 0.4's recorded public message remains parseable by Python 0.9.

    The fixture records public SPAKE2 messages from the interop prototype.
    It intentionally does not claim an equality check for the recorded derived
    key: reproducing that value would require the private ephemeral scalar
    from the original Rust process, which is not part of a public vector.
    """
    fixture = Path(__file__).with_name("fixtures") / "spake2-rust-0.4-vector.json"
    vector = json.loads(fixture.read_text())
    password = hashlib.sha256(PAIRING_IDENTITY + b"\0" + vector["code"].encode()).digest()
    assert password.hex() == vector["pw_hex"]
    assert bytes.fromhex(vector["identity_hex"]) == PAIRING_IDENTITY

    rust_message = bytes.fromhex(vector["python_first"]["rust_message_hex"])
    assert rust_message.startswith(b"S")
    pairing = SPAKE2_Symmetric(password, idSymmetric=PAIRING_IDENTITY)
    pairing.start()
    assert len(pairing.finish(rust_message)) == 32
    assert len(bytes.fromhex(vector["python_first"]["derived_key_hex"])) == 32
    assert len(bytes.fromhex(vector["rust_first"]["derived_key_hex"])) == 32


def _enable_pairing() -> str:
    runtime.pairing.disable()
    runtime.pairing.enable()
    return runtime.pairing.issue_code()


def _start(
    client: TestClient,
    msg_c: bytes,
    *,
    mode: str = "verified",
    host: str = "studio.local",
):
    body = {"mode": mode, "msg_c": _encoded(msg_c)}
    if mode == "verified":
        body["host"] = host
    return client.post("/api/devices/pair/start", json=body)


def _finish(client: TestClient, exchange_id: str, confirm_c: bytes):
    return client.post(
        "/api/devices/pair/finish",
        json={"exchange_id": exchange_id, "confirm_c": _encoded(confirm_c)},
    )


def test_disabled_pair_start_has_no_listener_or_key_side_effect():
    runtime.pairing.disable()
    assert runtime.listener is None
    key_path, cert_path = _key_paths()
    assert not key_path.exists()
    assert not cert_path.exists()

    refused = _start(TestClient(app), b"client", mode="pinned")

    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "expired"
    assert runtime.listener is None
    assert runtime.snapshot()["announced_name"] is None
    assert not key_path.exists()
    assert not cert_path.exists()


def test_pair_api_finishes_verified_exchange_with_canonical_host_vector():
    client_pairing = _client_pairing(_enable_pairing())
    msg_c = client_pairing.start()
    client = TestClient(app)
    started = _start(client, msg_c, host="Studio.Local.")
    assert started.status_code == 200
    payload = started.json()
    msg_s = base64.b64decode(payload["msg_s"])
    client_key, server_key = _split_key(client_pairing.finish(msg_s))
    transcript = PAIRING_IDENTITY + b"verified:studio.local\0" + msg_c + msg_s
    confirm_c = hmac.new(client_key, transcript, hashlib.sha256).digest()

    finished = _finish(client, payload["exchange_id"], confirm_c)

    assert finished.status_code == 200
    assert hmac.compare_digest(
        base64.b64decode(finished.json()["confirm_s"]),
        hmac.new(server_key, transcript, hashlib.sha256).digest(),
    )
    repeated = _finish(client, payload["exchange_id"], confirm_c)
    assert repeated.status_code == 409
    assert repeated.json()["detail"]["reason"] == "expired"


def test_pair_api_client_rejects_confirm_c_replayed_as_confirm_s():
    client_pairing = _client_pairing(_enable_pairing())
    msg_c = client_pairing.start()
    client = TestClient(app)
    started = _start(client, msg_c)
    assert started.status_code == 200
    payload = started.json()
    msg_s = base64.b64decode(payload["msg_s"])
    client_key, server_key = _split_key(client_pairing.finish(msg_s))
    transcript = PAIRING_IDENTITY + b"verified:studio.local\0" + msg_c + msg_s
    confirm_c = hmac.new(client_key, transcript, hashlib.sha256).digest()

    finished = _finish(client, payload["exchange_id"], confirm_c)

    assert finished.status_code == 200
    expected_confirm_s = hmac.new(server_key, transcript, hashlib.sha256).digest()
    assert not hmac.compare_digest(confirm_c, expected_confirm_s)
    assert not hmac.compare_digest(confirm_c, base64.b64decode(finished.json()["confirm_s"]))


def test_pair_api_rejects_fifth_pending_exchange_with_429():
    _enable_pairing()
    client = TestClient(app)
    for index in range(4):
        started = _start(client, f"client-{index}".encode())
        assert started.status_code == 200

    capped = _start(client, b"over-cap")

    assert capped.status_code == 429
    assert capped.json()["detail"]["reason"] == "too_many_pending"


def test_pair_api_failed_finish_consumes_exchange():
    client_pairing = _client_pairing(_enable_pairing())
    client = TestClient(app)
    started = _start(client, client_pairing.start())
    assert started.status_code == 200

    failed = _finish(client, started.json()["exchange_id"], b"not-a-confirmation")
    repeated = _finish(client, started.json()["exchange_id"], b"not-a-confirmation")

    assert failed.status_code == 401
    assert failed.json()["detail"]["reason"] == "wrong_code"
    assert repeated.status_code == 409
    assert repeated.json()["detail"]["reason"] == "invalid_exchange"


def test_pair_api_fifth_failed_finish_ends_code():
    _enable_pairing()
    client = TestClient(app)
    responses = []
    for _ in range(5):
        client_pairing = _client_pairing(runtime.pairing.status(include_code=True)["value"])
        started = _start(client, client_pairing.start())
        assert started.status_code == 200
        responses.append(_finish(client, started.json()["exchange_id"], b"wrong-confirmation"))

    assert [response.status_code for response in responses] == [401, 401, 401, 401, 409]
    assert responses[-1].json()["detail"]["reason"] == "too_many_tries"
    assert runtime.pairing.status()["state"] == "none"


def test_pair_api_finishes_pinned_exchange_with_spki_binding(monkeypatch):
    server_spki = bytes(range(32))
    monkeypatch.setattr(devices_api, "host_spki_sha256", lambda: server_spki)
    client_pairing = _client_pairing(_enable_pairing())
    msg_c = client_pairing.start()
    client = TestClient(app)
    started = _start(client, msg_c, mode="pinned")
    assert started.status_code == 200
    payload = started.json()
    msg_s = base64.b64decode(payload["msg_s"])
    client_key, server_key = _split_key(client_pairing.finish(msg_s))
    transcript = PAIRING_IDENTITY + b"pinned:" + server_spki + b"\0" + msg_c + msg_s
    confirm_c = hmac.new(client_key, transcript, hashlib.sha256).digest()

    finished = _finish(client, payload["exchange_id"], confirm_c)

    assert finished.status_code == 200
    assert hmac.compare_digest(
        base64.b64decode(finished.json()["confirm_s"]),
        hmac.new(server_key, transcript, hashlib.sha256).digest(),
    )


def test_pair_api_rejects_reflected_message_in_pinned_mode(monkeypatch):
    monkeypatch.setattr(devices_api, "host_spki_sha256", lambda: bytes(32))
    _enable_pairing()
    client = TestClient(app)
    first = _start(client, b"client", mode="pinned")
    assert first.status_code == 200

    reflected = _start(client, base64.b64decode(first.json()["msg_s"]), mode="pinned")

    assert reflected.status_code == 409
    assert reflected.json()["detail"]["reason"] == "reflected"


def test_pair_api_rejects_pinned_exchange_with_incorrect_spki_binding(monkeypatch):
    server_spki = bytes(range(32))
    incorrect_spki = bytes(reversed(range(32)))
    monkeypatch.setattr(devices_api, "host_spki_sha256", lambda: server_spki)
    client_pairing = _client_pairing(_enable_pairing())
    msg_c = client_pairing.start()
    client = TestClient(app)
    started = _start(client, msg_c, mode="pinned")
    assert started.status_code == 200
    msg_s = base64.b64decode(started.json()["msg_s"])
    client_key, _ = _split_key(client_pairing.finish(msg_s))
    incorrect_transcript = PAIRING_IDENTITY + b"pinned:" + incorrect_spki + b"\0" + msg_c + msg_s
    incorrect_confirm_c = hmac.new(client_key, incorrect_transcript, hashlib.sha256).digest()

    refused = _finish(client, started.json()["exchange_id"], incorrect_confirm_c)

    assert refused.status_code == 401
    assert refused.json()["detail"]["reason"] == "wrong_code"
