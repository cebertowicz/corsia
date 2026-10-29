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
    monkeypatch.setattr(flow, "local_setup_memory_ok", lambda: False)

    result = asyncio.run(flow.begin_local_setup())

    assert result["ok"] is False
    assert local_setup.flight_task() is None


def test_installed_server_requires_a_matching_managed_catalog_row(monkeypatch):
    from llm import local_sources

    monkeypatch.setattr(local_sources, "corsia_omlx_server", lambda **kwargs: {"id": "managed"})
    monkeypatch.setattr(local_sources, "list_models", lambda **kwargs: [{"litellm_model": "omlx@other/m"}])
    assert local_sources.installed_managed_server() is None
    monkeypatch.setattr(local_sources, "list_models", lambda **kwargs: [{"litellm_model": "omlx@managed/m"}])
    assert local_sources.installed_managed_server()["id"] == "managed"


@pytest.mark.usefixtures("mac_local_model_host")
@pytest.mark.parametrize("installed", [True, False])
def test_low_memory_projection_distinguishes_stopped_installed_server(monkeypatch, installed):
    from llm import local_sources

    server = {"id": "managed", "status": "disabled"}
    monkeypatch.setattr(local_sources, "local_setup_memory_ok", lambda: False)
    monkeypatch.setattr(local_sources, "local_omlx_servers", lambda *_args: [])
    monkeypatch.setattr(local_sources, "installed_managed_server", lambda: server if installed else None)
    monkeypatch.setattr(local_sources, "managed_omlx_executable", lambda: "/omlx")
    monkeypatch.setattr(local_sources, "managed_omlx_install_error", lambda: None)
    source = local_sources._omlx_state((set(), set()), [])
    assert source["eligible"] is False
    assert (source.get("reason_code") == "not_running") is installed


def test_stopped_installed_model_restarts_below_floor_without_downloading(monkeypatch):
    from llm import local_sources

    monkeypatch.setattr("llm.local_setup_gates.host_ram_bytes", lambda: 24 * 1024**3)
    monkeypatch.setattr(local_sources, "connected_local_server", lambda: None)
    monkeypatch.setattr(local_sources, "installed_managed_server", lambda: {"id": "managed"})
    monkeypatch.setattr(local_setup, "_claim_operation", lambda *_args, **_kwargs: True)
    calls = []

    async def lifecycle(server_id, action):
        calls.append((server_id, action))
        return {"ok": True}

    async def discover(server_id):
        calls.append((server_id, "discover"))
        return {"ok": True}

    async def no_download(*_args, **_kwargs):
        pytest.fail("restart must not download a model")

    monkeypatch.setattr("api.network_server_lifecycle.lifecycle_action", lifecycle)
    monkeypatch.setattr("llm.catalog.discover_network_models", discover)
    monkeypatch.setattr(local_setup, "_download", no_download)

    async def scenario():
        result = await flow.begin_local_setup()
        assert result["in_progress"]
        assert (await local_setup.flight_task())["existing"]

    asyncio.run(scenario())
    assert calls == [("managed", "start"), ("managed", "discover")]


def test_setup_uses_one_managed_flight_for_overlapping_requests(monkeypatch):
    monkeypatch.setattr(flow, "local_setup_memory_ok", lambda: True)
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
    monkeypatch.setattr(flow, "local_setup_memory_ok", lambda: True)
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
