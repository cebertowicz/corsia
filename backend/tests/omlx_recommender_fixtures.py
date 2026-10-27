"""A Mac whose managed oMLX answers from the recorded recommender, with nothing installed or downloaded.

The recommender answers with the recorded oMLX v0.7.1rc1 output
(``fixtures/omlx_recommendations_v0.7.1rc1.json``) through a fake ``admin_request``,
and every step that would install, start or download is replaced.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm import local_setup, local_setup_operation

GIB = 1024**3
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "omlx_recommendations_v0.7.1rc1.json").read_text())
# The recording predates the quality-first pick, which now lands on Gemma 4 31B at 32-64 GiB: same family as 26B, so same default.
THINKING = {**FIXTURE["thinking_default"], "gemma-4-31B-it-oQ3e-mtp": False, "gemma-4-31B-it-oQ4e-mtp": False}


def best_rows(gib: int) -> list[dict]:
    """The recorded recommender's ``best`` rows for a Mac with ``gib`` of memory."""
    return FIXTURE["results"][str(gib)]["sections"]["best"]


class _Reply:
    def __init__(self, status_code: int, payload: object = None):
        self.status_code, self._payload = status_code, payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeOmlx:
    """CORSIA's managed oMLX as its admin API answers: the recorded recommender for one size.

    ``answers`` are served to recommender calls first, in order: an HTTP status,
    an exception to raise, or a payload; after them, the recorded ``best`` rows.
    """

    def __init__(self, gib: int, answers=()):
        self.best = best_rows(gib) if str(gib) in FIXTURE["results"] else []
        self.answers = list(answers)
        self.calls: list[tuple[str, str, object]] = []
        self.gate: asyncio.Event | None = None

    def asks(self) -> int:
        return sum(1 for _method, path, _body in self.calls if path == "/hf/recommendations")

    def puts(self) -> list[tuple[str, object]]:
        return [(path, body) for method, path, body in self.calls if method == "PUT"]

    async def admin_request(self, _cfg, method, path, *, json=None, timeout=None):
        self.calls.append((method, path, json))
        if path == "/hf/recommendations":
            if self.gate is not None:
                await self.gate.wait()
            answer = self.answers.pop(0) if self.answers else {"sections": {"best": self.best}}
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, int):
                return _Reply(answer, {"detail": "recommender failed"})
            return _Reply(200, answer)
        if (method, path) == ("GET", "/models"):
            return _Reply(200, {"models": [{"id": m, "thinking_default": t} for m, t in THINKING.items()]})
        if method == "PUT":
            return _Reply(200, {"success": True})
        raise AssertionError(f"unexpected oMLX call {method} {path}")


@pytest.fixture
def fresh_local_setup(monkeypatch):
    """No setup flight, Operation record or person's own oMLX carried between tests."""
    local_setup_operation.clear()
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup, "_emit", lambda action, **properties: None)
    # The fixture uses a private managed oMLX flight.
    yield
    local_setup_operation.clear()


def mac_with(monkeypatch, gib: float, answers=()) -> SimpleNamespace:
    """A Mac with ``gib`` of memory whose managed oMLX answers from the recording."""
    import infra.state.ws
    import llm.catalog_network_models
    import llm.local_setup_flow

    mac = SimpleNamespace(omlx=FakeOmlx(int(gib), answers), started=[], health=[], downloads=[], stopped=[])

    async def start(report):
        mac.started.append(True)
        return "srv", 5200


    async def health(port):
        mac.health.append(port)

    async def download(server_id, report, model):
        mac.downloads.append(model)

    async def stop(config):
        mac.stopped.append(config["server_id"])
        return {"ok": True}

    async def discover(_server_id):
        return {"ok": True}

    monkeypatch.setattr("llm.local_setup_gates.platform.system", lambda: "Darwin")
    monkeypatch.setattr("infra.host_resources.host_total_ram_bytes", lambda: int(gib * GIB))
    # Nothing else is running, so the recorded "best" pick fits whatever host runs the test.
    monkeypatch.setattr("infra.host_resources.host_available_ram_bytes", lambda: int(gib * GIB))
    from llm.local_setup_gates import host_ram_bytes
    host_ram_bytes.cache_clear()
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", mac.omlx.admin_request)
    monkeypatch.setattr(llm.local_setup_flow, "start_managed_omlx", start)
    monkeypatch.setattr("llm.local_setup_prepare.start_managed_omlx", start)
    # start_managed_omlx owns the managed process lifecycle and supplies its port above.
    monkeypatch.setattr(local_setup, "_download", download)
    monkeypatch.setattr("llm.local_setup_runtime.wait_for_health", health)
    monkeypatch.setattr("llm.providers.omlx_supervisor.stop_server", stop)
    monkeypatch.setattr(llm.catalog_network_models, "discover_network_models", discover)
    monkeypatch.setattr(infra.state.ws, "notify_models_changed", lambda: None)
    return mac
