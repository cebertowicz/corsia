from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
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


def _rust_vector() -> dict:
    """Rust spake2 0.4's seeded vector, from fixtures/spake2-vector-gen."""
    fixture = Path(__file__).with_name("fixtures") / "spake2-rust-0.4-vector.json"
    return json.loads(fixture.read_text())


def _seeded_pairing(code: str, entropy_hex: str) -> SPAKE2_Symmetric:
    """A Python peer whose only randomness is the vector's 64 entropy bytes."""
    entropy = [bytes.fromhex(entropy_hex)]

    def entropy_f(count: int) -> bytes:
        assert entropy and count == len(entropy[0]), "spake2 must draw the fixture bytes once"
        return entropy.pop()

    secret = hashlib.sha256(PAIRING_IDENTITY + b"\0" + code.encode()).digest()
    return SPAKE2_Symmetric(secret, idSymmetric=PAIRING_IDENTITY, entropy_f=entropy_f)


@pytest.mark.parametrize("order", ["python_first", "rust_first"])
def test_python_spake2_derives_rust_04_vector_key(order):
    """Python 0.9 and Rust 0.4 agree on the exact key for one seeded exchange.

    The generator gives the Rust peer a ChaCha20 seed and computes the
    message the Python peer must send from the same entropy this test feeds
    Python. Its derived key is the Rust peer's finish(python_message).
    """
    vector = _rust_vector()
    secret = hashlib.sha256(PAIRING_IDENTITY + b"\0" + vector["code"].encode()).digest()
    assert secret.hex() == vector["pw_hex"]
    assert bytes.fromhex(vector["identity_hex"]) == PAIRING_IDENTITY
    exchange = vector[order]
    python_message = bytes.fromhex(exchange["python_message_hex"])
    rust_message = bytes.fromhex(exchange["rust_message_hex"])
    # The symmetric transcript sorts the two messages; cover both branches.
    assert (python_message[1:] < rust_message[1:]) is (order == "python_first")

    pairing = _seeded_pairing(vector["code"], exchange["python_entropy_hex"])

    assert pairing.start() == python_message
    assert pairing.finish(rust_message) == bytes.fromhex(exchange["derived_key_hex"])


def test_python_first_vector_has_a_fixed_name_bound_confirm_s():
    vector = _rust_vector()
    exchange = vector["python_first"]
    _, server_key = _split_key(bytes.fromhex(exchange["derived_key_hex"]))
    transcript = (
        PAIRING_IDENTITY
        + b"verified:studio.local\0"
        + bytes.fromhex(exchange["python_message_hex"])
        + bytes.fromhex(exchange["rust_message_hex"])
    )

    confirm_s = hmac.new(server_key, transcript + b"name:Studio", hashlib.sha256).digest()

    assert confirm_s.hex() == "944a40f6df11c32fd42a3a2652610d84113da85571c5d6f62a9e8867423be7e0"


def test_python_spake2_wrong_code_disagrees_with_rust_04_vector():
    vector = _rust_vector()
    wrong = vector["wrong_code"]
    exchange = vector[wrong["exchange"]]
    pairing = _seeded_pairing(wrong["code"], exchange["python_entropy_hex"])

    wrong_message = pairing.start()
    key = pairing.finish(bytes.fromhex(exchange["rust_message_hex"]))

    assert wrong_message == bytes.fromhex(wrong["python_message_hex"])
    assert wrong_message != bytes.fromhex(exchange["python_message_hex"])
    assert key != bytes.fromhex(exchange["derived_key_hex"])
    assert key != bytes.fromhex(wrong["rust_derived_key_hex"])


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


def test_disabled_pair_start_has_no_listener_or_key_side_effect(tmp_path):
    runtime.pairing.disable()
    assert runtime.listener is None
    key_path, cert_path = _key_paths(tmp_path)
    assert not key_path.exists()
    assert not cert_path.exists()

    refused = _start(TestClient(app), b"client", mode="pinned")

    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "expired"
    assert runtime.listener is None
    assert runtime.snapshot()["announced_name"] is None
    assert not key_path.exists()
    assert not cert_path.exists()


def test_pair_api_finishes_verified_exchange_with_canonical_host_vector(monkeypatch):
    name = "Studio"
    monkeypatch.setattr(runtime, "_name_resolver", lambda _announcement: name)
    monkeypatch.setattr(
        runtime,
        "listener",
        SimpleNamespace(announcement=SimpleNamespace(announced_name=None, error="DNS-SD failed")),
    )
    client_pairing = _client_pairing(_enable_pairing())
    updates: list[str] = []
    monkeypatch.setattr(runtime.pairing, "_on_update", lambda: updates.append("updated"))
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
    expected_confirm_s = hmac.new(server_key, transcript + b"name:Studio", hashlib.sha256).digest()
    assert finished.json() == {"confirm_s": _encoded(expected_confirm_s), "name": name}
    assert updates == ["updated"]
    assert not hmac.compare_digest(
        base64.b64decode(finished.json()["confirm_s"]),
        hmac.new(server_key, transcript + b"name:Other", hashlib.sha256).digest(),
    )
    repeated = _finish(client, payload["exchange_id"], confirm_c)
    assert repeated.status_code == 409
    assert repeated.json()["detail"]["reason"] == "expired"
    assert updates == ["updated"]


def test_pair_finish_normalizes_and_caps_name_at_a_utf8_boundary(monkeypatch):
    source_name = "e\u0301" * 31 + "aé"
    expected_name = "é" * 31 + "a"
    monkeypatch.setattr(runtime, "_name_resolver", lambda _announcement: source_name)
    client_pairing = _client_pairing(_enable_pairing())
    msg_c = client_pairing.start()
    client = TestClient(app)
    started = _start(client, msg_c)
    payload = started.json()
    msg_s = base64.b64decode(payload["msg_s"])
    client_key, server_key = _split_key(client_pairing.finish(msg_s))
    transcript = PAIRING_IDENTITY + b"verified:studio.local\0" + msg_c + msg_s
    confirm_c = hmac.new(client_key, transcript, hashlib.sha256).digest()

    finished = _finish(client, payload["exchange_id"], confirm_c)

    assert finished.status_code == 200
    assert finished.json() == {
        "confirm_s": _encoded(
            hmac.new(
                server_key,
                transcript + b"name:" + expected_name.encode(),
                hashlib.sha256,
            ).digest()
        ),
        "name": expected_name,
    }
    assert len(expected_name.encode()) == 63


def test_pair_api_client_rejects_confirm_c_replayed_as_confirm_s(monkeypatch):
    monkeypatch.setattr(runtime, "_name_resolver", lambda _announcement: "studio.local")
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
    expected_confirm_s = hmac.new(server_key, transcript + b"name:studio.local", hashlib.sha256).digest()
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


def test_pair_api_failed_finish_consumes_exchange(monkeypatch):
    name = "not-disclosed.local"
    monkeypatch.setattr(runtime, "_name_resolver", lambda _announcement: name)
    client_pairing = _client_pairing(_enable_pairing())
    client = TestClient(app)
    started = _start(client, client_pairing.start())
    assert started.status_code == 200

    failed = _finish(client, started.json()["exchange_id"], b"not-a-confirmation")
    repeated = _finish(client, started.json()["exchange_id"], b"not-a-confirmation")

    assert failed.status_code == 401
    assert failed.json()["detail"] == {"reason": "wrong_code"}
    assert name not in failed.text
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
    name = "studio.local"
    server_spki = bytes(range(32))
    monkeypatch.setattr(devices_api, "host_spki_sha256", lambda: server_spki)
    monkeypatch.setattr(runtime, "_name_resolver", lambda _announcement: name)
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
        hmac.new(server_key, transcript + b"name:studio.local", hashlib.sha256).digest(),
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
