"""A setup load failure ends once its model is really loaded on CORSIA's own server."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from llm.local_model import LocalModel

MODEL = LocalModel("test/qwen", 1024, 32768)
REFUSED = {"ok": False, "cause": "load_refused", "verdict": "no_memory", "ref": "local-1", "catalog_line": "Not loaded"}


@pytest.fixture
def flight(isolated_db, monkeypatch, tmp_path):
    """A failed setup flight as a restart restores it from its record."""
    from llm import local_setup, local_setup_operation as records

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr("infra.state.ws.schedule_models_changed", lambda: None)
    monkeypatch.setattr(local_setup, "_emit", lambda *_args, **_kwargs: None)

    def restore(result=REFUSED, *, kind="setup"):
        operation = replace(records.Operation.for_model(MODEL, kind, onboarding=True), result=result, downloaded=True)
        records.write(operation)
        local_setup.restore_terminal_flight(operation)

    return restore


def _own_server():
    from llm.managed_omlx_binary import MANAGED_INSTANCE_ID

    return {"id": "own", "provider": "omlx", "kind": "managed", "source": "corsia", "instance_id": MANAGED_INSTANCE_ID}


def test_a_refused_load_stays_a_failure_while_the_model_is_not_loaded(flight):
    from llm import local_setup, local_setup_operation as records

    flight()
    local_setup.settle_load_failure(_own_server(), {"another-model"})

    assert local_setup.setup_status()["state"] == "start_failed"
    assert local_setup.setup_status()["fix"]
    assert records.read().result == REFUSED


def test_a_loaded_model_ends_the_failure_and_keeps_it_ended_when_it_unloads(flight):
    from llm import local_setup, local_setup_operation as records

    flight()
    local_setup.settle_load_failure(_own_server(), {MODEL.model})

    assert local_setup.setup_status()["state"] == "ready"
    assert records.read().result["ok"] is True
    assert records.read().downloaded is True

    local_setup.settle_load_failure(_own_server(), set())
    assert local_setup.setup_status()["state"] == "ready"


def test_the_picker_stops_projecting_the_failure_once_the_model_is_loaded(flight, monkeypatch):
    from llm import local_setup, local_sources

    monkeypatch.setattr(local_sources, "local_memory_ok", lambda: True)
    monkeypatch.setattr(local_sources, "host_never_runs_local", lambda: False)
    monkeypatch.setattr("llm.local_setup_prepare.known_model", lambda: MODEL)
    monkeypatch.setattr(local_sources, "local_omlx_servers", lambda _rows: [{**_own_server(), "status": "online"}])
    flight()

    def row() -> dict:
        return local_sources._omlx_state(membership=({"omlx@own"}, {"omlx@own"}), omlx_rows=[])

    assert row()["state"] == "ready"
    assert row()["local_state"] == "start_failed"
    local_setup.settle_load_failure(_own_server(), {MODEL.model})
    assert row()["state"] == "ready"
    assert row()["local_state"] == "stopped"


@pytest.mark.asyncio
async def test_the_first_run_wake_reports_ready_not_the_stale_failure(flight, monkeypatch):
    from llm import local_setup
    from onboarding import local_download_wake as wake

    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", delivered)
    monkeypatch.setattr("infra.corsia_agent.get_corsia_agent", lambda: "conductor")
    monkeypatch.setattr(wake, "_first_run_rendered", lambda _thread_id: True)
    flight()

    local_setup.settle_load_failure(_own_server(), {MODEL.model})

    assert await wake.flush_pending_local_download("conductor") is True
    assert delivered.await_args.kwargs["chip"] == "Local model download ready."
    assert "load_refused" not in delivered.await_args.kwargs["notice"]


def test_a_persons_own_server_with_the_same_model_leaves_the_failure(flight):
    from llm import local_setup

    flight()
    local_setup.settle_load_failure({**_own_server(), "source": "manual", "instance_id": "lan"}, {MODEL.model})

    assert local_setup.setup_status()["state"] == "start_failed"


@pytest.mark.parametrize(("result", "kind"), [
    ({"ok": False, "cause": "disk_full", "bytes_to_free": 2048}, "setup"),
    ({"ok": False, "cause": "connection"}, "setup"),
    (REFUSED, "settings"),
])
def test_other_failures_and_settings_flights_are_left_alone(flight, result, kind):
    from llm import local_setup, local_setup_operation as records

    flight(result, kind=kind)
    local_setup.settle_load_failure(_own_server(), {MODEL.model})

    assert records.read().result == result


@pytest.mark.asyncio
async def test_the_managed_sweep_settles_the_failure_from_what_the_server_reports(flight, isolated_db):
    from llm import local_setup, network_discovery as nd

    flight()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "loaded_models": [MODEL.model]}, request=request)

    from llm.managed_omlx_binary import MANAGED_INSTANCE_ID

    server_id = nd.upsert_server("omlx", MANAGED_INSTANCE_ID, "CORSIA", "127.0.0.1", 5200, "online",
                                 kind="managed", source="corsia")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await nd._sweep_managed(client, nd.get_server(server_id))

    assert local_setup.setup_status()["state"] == "ready"
