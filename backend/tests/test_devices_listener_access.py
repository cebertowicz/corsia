"""G7 — actual TLS Devices listener preserves the main API's access boundary."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time

import httpx
import pytest
import websockets


_BACKEND = Path(__file__).resolve().parents[1]
_PUBLIC_PAIRS = frozenset({
    ("GET", "/api/health"),
    ("GET", "/api/version"),
    ("GET", "/api/auth/status"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/devices/pair/start"),
    ("POST", "/api/devices/pair/finish"),
    ("GET", "/api/devices/outside-address"),
    ("POST", "/api/devices/identity"),
})
_PASSWORD = "g7-disposable-listener-password"
_ACCOUNT = {
    "name": "G7 Listener Test",
    "username": "g7-listener",
    "password": _PASSWORD,
    "email": "g7-listener@example.test",
}


def _concrete_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "g7", path)


def _effective_main_api_pairs() -> list[tuple[str, str]]:
    """Enumerate effective HTTP routes only; websocket routes have their own auth proof."""
    from main import app
    from tools.conductor_api_policy import registered_routes

    pairs = {
        (method, _concrete_path(path))
        for route in registered_routes(app)
        if (path := getattr(route, "path", "")).startswith("/api")
        for method in set(getattr(route, "methods", ()) or ()) - {"HEAD", "OPTIONS"}
    }
    assert pairs
    return sorted(pairs)


_MAIN_API_PAIRS = _effective_main_api_pairs()


class _NoopAnnouncement:
    """The access test owns TLS and Uvicorn, not machine-wide DNS-SD registration."""

    def __init__(self, _port: int):
        self.announced_name = None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _loopback_socket() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(socket.SOMAXCONN)
    listener.setblocking(False)
    return listener


def _fixture_main() -> None:
    """Run the real main app plus DevicesListener under isolated process state."""
    assert os.environ["CORSIA_TEST_MODE"] == "1"
    assert os.environ["CORSIA_ANALYTICS_DISABLE"] == "1"
    workspace = Path(os.environ["CORSIA_WORKSPACE"]).resolve()
    state = Path(os.environ["CORSIA_STATE_DIR"]).resolve()
    assert workspace.is_dir() and state.is_dir() and workspace != state

    from fastapi import FastAPI
    import uvicorn
    from core.devices_runtime import runtime
    from infra import devices_listener
    import main as backend

    # The listener still runs its production TLS/Uvicorn/DevicesApp path. Only
    # discovery is suppressed and the bind is made ephemeral for this test.
    devices_listener.DNSServiceAnnouncement = _NoopAnnouncement

    def prebind():
        listener = _loopback_socket()
        return listener, listener.getsockname()[1], None

    devices_listener._prebind = prebind

    @asynccontextmanager
    async def lifespan(_app):
        async with backend.app.router.lifespan_context(backend.app):
            yield

    app = FastAPI(lifespan=lifespan)

    @app.post("/__g7/start-listener")
    async def start_listener():
        assert await runtime.enable(backend.app)
        assert runtime.listener is not None
        while not runtime.listener.server.started:
            await asyncio.sleep(0.001)
        return {"port": runtime.listener.port}

    app.mount("/", backend.app)
    sock = _loopback_socket()
    print("G7_MAIN_PORT " + str(sock.getsockname()[1]), flush=True)
    asyncio.run(uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on")).serve(sockets=[sock]))


class _AccessBackend:
    def __init__(self, root: Path):
        self.workspace = root / "workspace"
        self.state = root / "state"
        self.workspace.mkdir()
        self.state.mkdir()
        self.log = root / "backend.log"
        env = {
            **os.environ,
            "CORSIA_WORKSPACE": str(self.workspace),
            "CORSIA_STATE_DIR": str(self.state),
            "CORSIA_TEST_MODE": "1",
            "CORSIA_ANALYTICS_DISABLE": "1",
            "PYTHONPATH": str(_BACKEND),
        }
        with self.log.open("w") as output:
            self.process = subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).resolve()), "--g7-fixture"],
                cwd=_BACKEND,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            self._wait_for_main()
            self._start_listener()
        except BaseException:
            self.close()
            raise

    def _wait_for_main(self) -> None:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            text = self.log.read_text()
            if self.process.poll() is not None:
                raise AssertionError(f"isolated G7 backend exited {self.process.returncode}; log={self.log}")
            for line in text.splitlines():
                if line.startswith("G7_MAIN_PORT "):
                    self.main_url = f"http://127.0.0.1:{int(line.removeprefix('G7_MAIN_PORT '))}"
                    return
            time.sleep(0.02)
        raise AssertionError(f"isolated G7 backend startup timed out; log={self.log}")

    def _start_listener(self) -> None:
        with httpx.Client(base_url=self.main_url, timeout=10, trust_env=False) as client:
            response = client.post("/__g7/start-listener")
        assert response.status_code == 200, response.text
        port = response.json()["port"]
        assert isinstance(port, int) and port not in {1956, 3000, 3001, 47901}
        self.remote_url = f"https://127.0.0.1:{port}"
        self.remote_ws_url = f"wss://127.0.0.1:{port}"

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


@pytest.fixture(scope="module")
def access_backend():
    with tempfile.TemporaryDirectory(prefix="corsia-g7-") as directory:
        backend = _AccessBackend(Path(directory))
        try:
            yield backend
        finally:
            backend.close()


@pytest.mark.asyncio
async def test_devices_tls_listener_keeps_login_public_but_refuses_pairing_on_loopback(access_backend):
    async with httpx.AsyncClient(base_url=access_backend.main_url, timeout=10, trust_env=False) as main_client, \
            httpx.AsyncClient(base_url=access_backend.remote_url, verify=False, timeout=10, trust_env=False) as remote:
        # The direct main listener remains behaviorally unchanged.
        direct_models = await main_client.get("/api/models")
        assert direct_models.status_code == 200

        public = [
            await remote.get("/api/health"),
            await remote.get("/api/version"),
            await remote.get("/api/auth/status"),
            await remote.post("/api/auth/login", json={"username": "missing", "password": "wrong"}),
            await remote.post("/api/devices/pair/start", json={}),
            await remote.post("/api/devices/pair/finish", json={}),
        ]
        assert [response.status_code for response in public[:3]] == [200, 200, 200]
        assert public[3].status_code == 401
        assert public[3].json() != {"reason": "sign_in_required"}
        assert [response.status_code for response in public[4:]] == [403, 403]
        assert [response.json() for response in public[4:]] == [{"reason": "not_on_this_network"}] * 2

        for method, path in (("GET", "/api/auth/login"), ("POST", "/api/version")):
            refused = await remote.request(method, path)
            assert refused.status_code == 401
            assert refused.json() == {"reason": "sign_in_required"}

        created = await main_client.post("/api/setup/first-account", json=_ACCOUNT)
        assert created.status_code == 200, created.text
        login = await remote.post("/api/auth/login", json={
            "username": _ACCOUNT["username"], "password": _PASSWORD,
        })
        assert login.status_code == 200, login.text
        token = login.json()["token"]

        for method, path in (
            ("GET", "/api/models"),
            ("GET", "/api/models/not-a-model/profile"),
            ("POST", "/api/upload"),
        ):
            refused = await remote.request(method, path)
            assert refused.status_code == 401
            assert refused.json() == {"reason": "sign_in_required"}

        forwarded = await remote.get("/api/models", headers={"Authorization": f"Bearer {token}"})
        assert forwarded.status_code == 200
        assert forwarded.json() == direct_models.json()


@pytest.mark.asyncio
async def test_devices_tls_listener_proves_identity_with_the_key_its_tls_presents(access_backend):
    """A signed-out caller on the real listener gets a proof the desktop app can check against the TLS key."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    port = int(access_backend.remote_url.rsplit(":", 1)[1])
    presented = x509.load_pem_x509_certificate(ssl.get_server_certificate(("127.0.0.1", port)).encode())
    spki = presented.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    nonce = os.urandom(32)
    authority = "studio.example:443"
    async with httpx.AsyncClient(base_url=access_backend.remote_url, verify=False, timeout=10, trust_env=False) as remote:
        proof = await remote.post("/api/devices/identity", json={
            "nonce": base64.b64encode(nonce).decode(), "authority": authority,
        })
        refused = [
            await remote.post("/api/devices/identity", json={"nonce": "A" * 4000, "authority": authority}),
            await remote.post("/api/devices/identity", json={
                "nonce": base64.b64encode(nonce).decode(), "authority": "Studio.example:443",
            }),
        ]
    assert proof.status_code == 200, proof.text
    assert base64.b64decode(proof.json()["spki"]) == spki
    presented.public_key().verify(
        base64.b64decode(proof.json()["signature"]),
        b"corsia-identity-v1\0" + nonce + b"\0" + authority.encode(),
        ec.ECDSA(hashes.SHA256()),
    )
    assert [response.status_code for response in refused] == [400, 400]


@pytest.mark.parametrize(("method", "path"), _MAIN_API_PAIRS)
@pytest.mark.asyncio
async def test_devices_tls_listener_refuses_every_registered_unlisted_api_pair(access_backend, method, path):
    if (method, path) in _PUBLIC_PAIRS:
        pytest.skip("explicitly public Devices API pair")
    async with httpx.AsyncClient(base_url=access_backend.remote_url, verify=False, timeout=10, trust_env=False) as remote:
        response = await remote.request(method, path)
    assert response.status_code == 401, (method, path, response.text)
    assert response.json() == {"reason": "sign_in_required"}


@pytest.mark.asyncio
async def test_devices_tls_listener_keeps_existing_websocket_authentication(access_backend):
    tls = ssl.create_default_context()
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE

    async with websockets.connect(f"{access_backend.remote_ws_url}/ws", ssl=tls) as socket_:
        await socket_.send(json.dumps({"token": ""}))
        assert json.loads(await socket_.recv()) == {
            "type": "error", "message": "Invalid or expired session",
        }
        await socket_.wait_closed()
        assert socket_.close_code == 1000

    async with websockets.connect(f"{access_backend.remote_ws_url}/api/browser/g7/stream", ssl=tls) as socket_:
        await socket_.send(json.dumps({"token": ""}))
        await socket_.wait_closed()
        assert socket_.close_code == 1008


if __name__ == "__main__":
    assert sys.argv == [str(Path(__file__).resolve()), "--g7-fixture"]
    _fixture_main()
