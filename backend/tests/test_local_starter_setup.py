"""The single local-model setup flight for CORSIA's managed oMLX."""

from __future__ import annotations

import asyncio

import pytest

from llm import local_setup, local_setup_operation, local_setup_flow as flow
from llm.local_model import LocalModel

PICK = LocalModel("Jundot/Qwen3.8-27B-oQ4e-mtp", 16_971_681_558, 40_960)


@pytest.fixture(autouse=True)
def fresh_flight(monkeypatch, tmp_path):
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup_operation, "_state_root", lambda: tmp_path)
    monkeypatch.setattr(local_setup, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)


def test_ineligible_mac_does_not_start_a_managed_setup(monkeypatch):
    monkeypatch.setattr(flow, "local_memory_ok", lambda: False)

    result = asyncio.run(flow.begin_local_setup())

    assert result["ok"] is False
    assert local_setup.flight_task() is None


def test_setup_uses_one_managed_flight_for_overlapping_requests(monkeypatch):
    monkeypatch.setattr(flow, "local_memory_ok", lambda: True)
    monkeypatch.setattr(local_setup, "_claim_operation", lambda *_args, **_kwargs: True)
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[object] = []

    async def run_flight(*, model=None):
        runs.append(model)
        started.set()
        await release.wait()
        return {"ok": True, "model": PICK.model}

    monkeypatch.setattr(flow, "_run_flight", run_flight)

    async def scenario():
        first = await flow.begin_local_setup()
        await started.wait()
        second = await flow.begin_local_setup()
        assert first["in_progress"] is True
        assert second["in_progress"] is True
        release.set()
        assert await local_setup.flight_task() == {"ok": True, "model": PICK.model}

    asyncio.run(scenario())

    assert runs == [None]


def test_pending_operation_is_recovered_before_a_new_managed_flight(monkeypatch):
    monkeypatch.setattr(flow, "local_memory_ok", lambda: True)
    local_setup_operation.write(local_setup_operation.Operation.for_model(PICK, "setup", "task-1"))
    recovered: list[bool] = []

    async def resume_pending_setup():
        recovered.append(True)
        return True

    monkeypatch.setattr("llm.local_setup_flow.resume_pending_setup", resume_pending_setup)

    result = asyncio.run(flow.begin_local_setup())

    assert result["in_progress"] is True
    assert recovered == [True]


@pytest.mark.usefixtures("mac_local_model_host")
def test_an_absent_managed_server_stays_not_installed_after_a_terminal_flight_failure(monkeypatch):
    from llm import local_sources

    local_setup._flight.result = {"ok": False, "message": "managed server failed"}
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])
    monkeypatch.setattr(local_sources, "omlx_on_path", lambda: None)

    assert local_sources.omlx_source((set(), set()))["state"] == "not_installed"
