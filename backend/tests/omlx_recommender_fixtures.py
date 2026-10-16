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
THINKING = FIXTURE["thinking_default"]


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

    def puts(self) -> list[tuple[str, dict]]:
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
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: False)
    yield
    local_setup_operation.clear()


def mac_with(monkeypatch, gib: float, answers=()) -> SimpleNamespace:
    """A Mac with ``gib`` of memory whose managed oMLX answers from the recording."""
    import infra.state.ws
    import llm.catalog_network_models
    import llm.local_setup_new_omlx

    mac = SimpleNamespace(omlx=FakeOmlx(int(gib), answers), started=[], health=[], downloads=[], commands=[])

    async def start(report):
        mac.started.append(True)
        return "srv", 5200

    async def answers_now(_port):
        return False

    async def healthy(port):
        mac.health.append(port)

    async def download(server_id, report, model):
        mac.downloads.append(model)

    async def discover(_server_id):
        return {"ok": True}

    monkeypatch.setattr("infra.host_resources.host_total_ram_bytes", lambda: int(gib * GIB))
    from llm.local_setup_gates import host_ram_bytes
    host_ram_bytes.cache_clear()
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", mac.omlx.admin_request)
    monkeypatch.setattr(llm.local_setup_new_omlx, "start_managed_omlx", start)
    monkeypatch.setattr(local_setup, "own_omlx_port", lambda: 5200)
    monkeypatch.setattr(local_setup, "_omlx_answers", answers_now)
    monkeypatch.setattr(local_setup, "_wait_for_health", healthy)
    monkeypatch.setattr(local_setup, "_run", lambda command: mac.commands.append(command))
    monkeypatch.setattr(local_setup, "_download", download)
    monkeypatch.setattr(llm.catalog_network_models, "discover_network_models", discover)
    monkeypatch.setattr(infra.state.ws, "notify_models_changed", lambda: None)
    return mac
