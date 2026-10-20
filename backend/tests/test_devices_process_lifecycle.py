"""Real two-server I9 proof, with fresh state and loopback ephemeral ports."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import tempfile
import time

import httpx
import pytest
import websockets


_BACKEND = Path(__file__).resolve().parents[1]
_FORBIDDEN_PORTS = {1956, 3000, 3001, 47901}


class BackendProcess:
    def __init__(self, root: Path):
        self.workspace = root / "workspace"
        self.state = root / "state"
        self.workspace.mkdir()
        self.state.mkdir()
        self.log = root / "backend.log"
        env = {**os.environ, "CORSIA_WORKSPACE": str(self.workspace),
               "CORSIA_STATE_DIR": str(self.state), "CORSIA_TEST_MODE": "1",
               "CORSIA_ANALYTICS_DISABLE": "1", "PYTHONPATH": str(_BACKEND)}
        with self.log.open("w") as output:
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("devices_process_fixture.py"))],
                cwd=_BACKEND, env=env, stdout=output, stderr=subprocess.STDOUT,
            )
        try:
            self._wait_for_ready()
        except BaseException:
            self.close()
            raise

    def _wait_for_ready(self):
        self.port = None
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            text = self.log.read_text()
            if self.process.poll() is not None:
                raise AssertionError(f"isolated backend exited {self.process.returncode}; log={self.log}")
            if "I9_READY" in text:
                lines = text.splitlines()
                identity = json.loads(next(line.removeprefix("I9_IDENTITY ") for line in lines
                                           if line.startswith("I9_IDENTITY ")))
                assert Path(identity["workspace"]).resolve() == self.workspace.resolve()
                assert Path(identity["state_root"]).resolve() == self.state.resolve()
                assert Path(identity["db_path"]).resolve().is_relative_to(self.state.resolve())
                assert identity["pid"] == self.process.pid
                self.port = int(next(line.removeprefix("I9_PORT ") for line in lines
                                     if line.startswith("I9_PORT ")))
                assert self.port not in _FORBIDDEN_PORTS
                self.identity = identity
                break
            time.sleep(0.02)
        assert self.port is not None, f"isolated backend startup timed out; log={self.log}"
        self.url = f"http://127.0.0.1:{self.port}"

    def terminate(self):
        started = time.monotonic()
        self.process.send_signal(signal.SIGTERM)
        code = self.process.wait(timeout=10)
        elapsed = time.monotonic() - started
        text = self.log.read_text()
        assert "timeout graceful shutdown exceeded" not in text.lower()
        assert "application shutdown failed" not in text.lower()
        assert "Exception in ASGI application" not in text
        assert "ASGI callable returned without completing response" not in text
        return code, elapsed

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


@contextmanager
def backend_process():
    with tempfile.TemporaryDirectory(prefix="corsia-i9-") as directory:
        backend = None
        try:
            backend = BackendProcess(Path(directory))
            yield backend
        finally:
            if backend is not None:
                backend.close()


@asynccontextmanager
async def open_streams(client):
    response = await client.put("/__i9/devices/true")
    response.raise_for_status()
    port = response.json()["port"]
    assert port not in _FORBIDDEN_PORTS
    tls = ssl.create_default_context()
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    async with httpx.AsyncClient(verify=tls, timeout=5, trust_env=False) as remote:
        async with remote.stream("GET", f"https://127.0.0.1:{port}/__i9/events") as sse:
            assert sse.status_code == 200
            lines = sse.aiter_lines()
            assert await anext(lines) == "data: ready"
            async with websockets.connect(f"wss://127.0.0.1:{port}/__i9/ws", ssl=tls) as ws:
                assert await ws.recv() == "ready"
                yield
                await asyncio.wait_for(ws.wait_closed(), timeout=2)
                assert ws.close_code == 1012


async def stable_counts(client, *, quiet_seconds=0.15):
    previous = None
    unchanged_since = time.monotonic()
    deadline = unchanged_since + max(2, quiet_seconds * 3)
    while time.monotonic() < deadline:
        response = await client.get("/__i9/counts")
        response.raise_for_status()
        counts = response.json()
        if counts != previous:
            unchanged_since = time.monotonic()
        elif time.monotonic() - unchanged_since >= quiet_seconds:
            return counts
        previous = counts
        await asyncio.sleep(0.05)
    raise AssertionError(f"backend resources never settled: {previous}")


@pytest.mark.integration
async def test_devices_open_streams_stop_under_grace_and_50_cycles_release_resources():
    with backend_process() as backend:
        async with httpx.AsyncClient(base_url=backend.url, timeout=5, trust_env=False) as client:
            # Native startup workers retire asynchronously. Select the exact
            # baseline only after a continuous quiet window, before cycle 1.
            baseline = await stable_counts(client, quiet_seconds=5)
            durations = []
            for cycle in range(50):
                async with open_streams(client):
                    started = time.monotonic()
                    response = await client.put("/__i9/devices/false")
                    durations.append(time.monotonic() - started)
                    response.raise_for_status()
                    assert durations[-1] < 2, (cycle, durations[-1])
                counts = await stable_counts(client)
                assert all(counts[k] <= baseline[k] for k in baseline), (
                    f"resource leak at cycle {cycle + 1}: {counts} > {baseline}"
                )
                # A startup worker can still retire after the quiet window. A
                # leak is growth, so later cycles compare against the new floor.
                baseline = counts
            final = await stable_counts(client)
            assert all(final[k] <= baseline[k] for k in baseline)
            print(f"I9 cycles=50 baseline={baseline} final={final} stop_max={max(durations):.4f}s")
        code, elapsed = backend.terminate()
        assert code in (0, -signal.SIGTERM)
        print(f"I9 cycle_process_exit={code} elapsed={elapsed:.4f}s identity={backend.identity}")


@pytest.mark.integration
async def test_devices_sigterm_with_open_streams_matches_no_devices_baseline():
    with backend_process() as baseline:
        async with httpx.AsyncClient(base_url=baseline.url, timeout=5, trust_env=False) as client:
            await stable_counts(client, quiet_seconds=5)
        baseline_code, baseline_time = baseline.terminate()
    with backend_process() as backend:
        async with httpx.AsyncClient(base_url=backend.url, timeout=5, trust_env=False) as client:
            await stable_counts(client, quiet_seconds=5)
            async with open_streams(client):
                # Keep the peer idle while the real backend handles SIGTERM.
                code, elapsed = backend.terminate()
        assert code == baseline_code
        assert elapsed < baseline_time + 2
        print(f"I9 SIGTERM baseline={baseline_code}/{baseline_time:.4f}s streams={code}/{elapsed:.4f}s")


async def test_devices_shutdown_finishes_before_storage_pool(monkeypatch):
    # The full lifespan shutdown must join devices before closing its database.
    from test_app_lifecycle_shutdown import _isolate_lifespan, _record_tail
    import app_lifecycle
    from core.devices_runtime import runtime

    _isolate_lifespan(monkeypatch)
    events = []
    _record_tail(monkeypatch, events)

    async def disable(_self):
        await asyncio.sleep(0)
        events.append("devices_joined")

    monkeypatch.setattr(type(runtime), "disable", disable)
    await app_lifecycle._shutdown_runtime_services([None] * 5)
    assert events.index("devices_joined") < events.index("db_pool")
