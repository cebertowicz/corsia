"""State and upgrade regressions across the setup flight's consumers."""

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def flight(monkeypatch, tmp_path):
    from llm import local_setup

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup, "_emit", lambda *_a, **_k: None)
    monkeypatch.setattr(local_setup, "_notify_models_changed", lambda: None)
    return local_setup


def test_signed_in_row_projection_uses_the_normalized_flight_shape(flight):
    from llm import local_sources
    from onboarding.local_download_state import normalize

    normalized = normalize({
        "state": "failed", "cause": "disk_full", "percent": 41, "eta_seconds": 80,
        "bytes_to_free": 4_000_000_000, "ref": "local-42", "fix": "The local model needs 4.0 GB more free space. Free it up, then try again.",
    })
    row = {"state": "needs_setup", "reason": None, "fix": None}

    local_sources._project_local_flight(row, normalized)

    assert row == {
        "state": "needs_setup", "reason": None, "fix": "The local model needs 4.0 GB more free space. Free it up, then try again.",
        "local_state": "failed", "setup_error_code": "disk_full", "setup_error_message": None, "setup_error_reference": "local-42",
        "bytes_to_free": 4_000_000_000, "download_percent": 41, "download_eta_seconds": 80,
        "download_stalled": False,
    }


@pytest.mark.asyncio
async def test_restart_failure_and_refusal_reach_the_source_projection(flight, monkeypatch):
    from llm import local_sources
    from llm.local_setup_flow import _restart_installed
    from onboarding.local_download_state import current

    lifecycle = AsyncMock(side_effect=[
        {"ok": False, "message": "Port is busy.", "ref": "local-123", "fix": "Free the port, then try again."},
        {"ok": False, "reason": "mac_cannot_run", "message": "No model fits. Connect a provider."},
    ])
    monkeypatch.setattr("api.network_server_lifecycle.lifecycle_action", lifecycle)
    for expected in ("Port is busy.", "No model fits. Connect a provider."):
        await _restart_installed("server")
        row = {}
        local_sources._project_local_flight(row, current())
        assert row["setup_error_message"] == expected
    assert row["setup_error_code"] == "mac_cannot_run"


@pytest.mark.asyncio
async def test_known_model_is_starting_until_an_accepted_download_and_starting_again_after_it(flight):
    from llm.local_profiles import STARTER_MODEL

    assert flight._claim_operation("setup", STARTER_MODEL)
    task = asyncio.create_task(asyncio.Event().wait())
    flight._flight.task = task
    try:
        assert flight.setup_status()["state"] == "starting"
        flight._record_download_task(STARTER_MODEL, "task-1")
        assert flight.setup_status()["state"] == "downloading"
        flight.record_downloaded()
        assert flight.setup_status()["state"] == "starting"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        flight.finish_flight({"ok": True})
    assert flight.setup_status()["state"] == "ready"


@pytest.mark.asyncio
async def test_startup_delivery_failure_has_traceback_reference_and_durable_failed_state(flight, monkeypatch, caplog):
    import app_startup_runtime
    from llm import local_setup_operation as records
    from onboarding import local_download_state

    operation = records.Operation.for_model(None, "setup", onboarding=True)
    records.write(operation)
    monkeypatch.setattr("llm.local_model.backfill_legacy_record", lambda: None)
    monkeypatch.setattr("llm.local_setup_flow.resume_pending_setup", AsyncMock(return_value=False))
    monkeypatch.setattr("onboarding.local_download_wake.resume_pending_local_download", AsyncMock(side_effect=RuntimeError("notice failed")))
    await app_startup_runtime.resume_local_setup()
    status = flight.setup_status()
    assert status["state"] == "failed"
    assert status["ref"].startswith("local-")
    assert local_download_state.current()["ref"] == status["ref"]
    assert records.read().result["ref"] == status["ref"]
    assert any(row.exc_info and f"reference={status['ref']}" in row.message for row in caplog.records)


@pytest.mark.asyncio
async def test_startup_resume_failure_has_traceback_reference_and_durable_failed_state(flight, monkeypatch, caplog):
    import app_startup_runtime
    from llm import local_setup_operation as records

    records.write(records.Operation.for_model(None, "setup", onboarding=True))
    monkeypatch.setattr("llm.local_model.backfill_legacy_record", lambda: None)
    monkeypatch.setattr("llm.local_setup_flow.resume_pending_setup", AsyncMock(side_effect=RuntimeError("resume failed")))
    monkeypatch.setattr("onboarding.local_download_wake.resume_pending_local_download", AsyncMock(return_value=None))

    await app_startup_runtime.resume_local_setup()

    status = flight.setup_status()
    assert status["state"] == "failed"
    assert status["ref"].startswith("local-")
    assert records.read().result["ref"] == status["ref"]
    assert any(row.exc_info and f"reference={status['ref']}" in row.message for row in caplog.records)


@pytest.mark.asyncio
async def test_startup_delivery_failure_preserves_a_ready_outcome(flight, monkeypatch):
    import app_startup_runtime
    from llm import local_setup_operation as records

    ready = replace(records.Operation.for_model(None, "setup", onboarding=True), result={"ok": True})
    records.write(ready)
    monkeypatch.setattr("llm.local_model.backfill_legacy_record", lambda: None)
    monkeypatch.setattr("llm.local_setup_flow.resume_pending_setup", AsyncMock(return_value=False))
    monkeypatch.setattr("onboarding.local_download_wake.resume_pending_local_download", AsyncMock(side_effect=RuntimeError("notice failed")))

    await app_startup_runtime.resume_local_setup()

    assert records.read().result == {"ok": True}
    assert flight._flight.task is None


@pytest.mark.asyncio
async def test_startup_delivery_failure_preserves_a_live_flight(flight, monkeypatch):
    import app_startup_runtime
    from llm import local_setup_operation as records

    records.write(records.Operation.for_model(None, "setup", onboarding=True))
    task = asyncio.create_task(asyncio.Event().wait())
    flight._flight.task = task
    monkeypatch.setattr("llm.local_model.backfill_legacy_record", lambda: None)
    monkeypatch.setattr("llm.local_setup_flow.resume_pending_setup", AsyncMock(return_value=True))
    monkeypatch.setattr("onboarding.local_download_wake.resume_pending_local_download", AsyncMock(side_effect=RuntimeError("notice failed")))
    try:
        await app_startup_runtime.resume_local_setup()
        assert records.read().result is None
        assert flight._flight.task is task and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_setup_download_registers_then_forces_a_pin_and_read_back(flight, monkeypatch):
    from llm import local_setup_flow as flow
    from llm.local_model import LocalModel

    model = LocalModel("Jundot/new-model", 1_000_000, 32_768)
    keep_loaded = AsyncMock()
    monkeypatch.setattr(flow.prepare, "join", AsyncMock())
    monkeypatch.setattr("llm.local_sources.local_server_for_install", AsyncMock(return_value=("server", 8210, False)))
    monkeypatch.setattr(flow.ls, "_record_model", lambda _model: None)
    monkeypatch.setattr(flow.ls, "_download", AsyncMock())
    monkeypatch.setattr(flow.ls, "record_downloaded", lambda: None)
    monkeypatch.setattr(flow, "apply_optimal_settings", AsyncMock(return_value=SimpleNamespace(status="applied", writer_attempted=True)))
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(flow, "record_set_up", lambda _model: None)
    monkeypatch.setattr(flow, "keep_loaded", keep_loaded)

    result = await flow._setup_new_omlx(lambda _stage, **_kwargs: None, [], model)

    assert result["ok"] is True
    keep_loaded.assert_awaited_once_with("server", model, force_pin=True)


@pytest.mark.asyncio
@pytest.mark.usefixtures("mac_local_model_host")
async def test_corsias_own_registered_local_model_is_visible_and_setup_and_prepare_do_not_modify_it(isolated_db, flight, monkeypatch):
    from llm import local_setup_flow as flow, local_setup_prepare as prepare, local_sources
    from llm import network_discovery as discovery
    from llm.managed_omlx_binary import MANAGED_INSTANCE_ID
    from test_managed_omlx_ownership import _model_row

    sid = discovery.upsert_server(provider="omlx", instance_id=MANAGED_INSTANCE_ID, instance_name="Existing oMLX",
                                  host="127.0.0.1", port=8000, status="online", kind="managed", source="corsia")
    _model_row("old-local-model", f"omlx@{sid}/existing/model", "existing/model")
    monkeypatch.setattr(prepare, "_prepare", prepare._Prepare())
    monkeypatch.setattr(prepare, "start_managed_omlx", AsyncMock(side_effect=AssertionError("must not start oMLX")))
    monkeypatch.setattr(flow, "start_managed_omlx", AsyncMock(side_effect=AssertionError("must not start oMLX")))
    monkeypatch.setattr(prepare, "local_setup_memory_ok", lambda: True)
    monkeypatch.setattr(flow, "local_setup_memory_ok", lambda: True)
    before = discovery.get_server(sid)
    assert local_sources.omlx_source()["state"] == "ready"
    await prepare.begin_prepare()
    assert prepare.prepare_task() is None
    result = await flow.begin_local_setup(onboarding=True)
    assert result["ok"] and result["existing"] and result["server_id"] == sid
    assert flight.setup_status()["state"] == "ready"
    assert (await flow._setup_new_omlx(lambda _s: None, []))["existing"]
    assert discovery.get_server(sid) == before
    assert len(discovery.get_servers(provider="omlx")) == 1


@pytest.mark.asyncio
async def test_a_persons_own_omlx_is_left_alone_and_never_stands_in_for_corsias_server(isolated_db, flight, monkeypatch):
    from llm import local_sources
    from llm import network_discovery as discovery
    from test_managed_omlx_ownership import _model_row

    sid = discovery.upsert_server(provider="omlx", instance_id="127.0.0.1:8000", instance_name="My oMLX",
                                  host="127.0.0.1", port=8000, status="online", kind="managed")
    _model_row("their-model", f"omlx@{sid}/their/model", "their/model")
    before = discovery.get_server(sid)

    assert await local_sources.local_server_for_install(lambda _s: None, start_if_absent=False) is None
    source = local_sources.omlx_source()
    assert source["corsia_omlx"] is False and source["corsia_omlx_online"] is False
    assert discovery.get_server(sid) == before


@pytest.mark.asyncio
async def test_failed_boot_retains_identity_until_the_supervised_process_exits(flight, monkeypatch):
    from llm.providers import omlx_supervisor as supervisor

    pid = os.getpid()
    supervisor._write_pid("boot-failed", pid)
    monkeypatch.setattr(supervisor, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(supervisor, "_process_exited", lambda _pid: False)
    monkeypatch.setattr(supervisor.os, "kill", lambda *_args: None)
    monkeypatch.setattr(supervisor, "_tail_log", lambda *_a, **_k: "")
    monkeypatch.setattr(supervisor, "_port_owner_pid", lambda *_args: None)
    monkeypatch.setattr(supervisor, "_spawn_process", lambda *_args: pytest.fail("must not lose a live spawn"))
    assert supervisor._failed_boot_result("boot-failed", "127.0.0.1", 8210, pid)["ok"] is False
    assert supervisor.is_corsia_process(pid)
    assert (await supervisor.start_server({"server_id": "boot-failed", "host": "127.0.0.1", "port": 8210}))["ok"] is False
    assert supervisor.is_corsia_process(pid)


@pytest.mark.asyncio
async def test_a_running_server_with_no_model_starts_the_download_instead_of_reading_as_connected(isolated_db, flight, monkeypatch):
    from llm import local_setup_flow as flow
    from llm import network_discovery as discovery
    from llm.managed_omlx_binary import MANAGED_INSTANCE_ID

    discovery.upsert_server(provider="omlx", instance_id=MANAGED_INSTANCE_ID, instance_name="Local model",
                            host="127.0.0.1", port=8210, status="online", kind="managed", source="corsia")
    started = asyncio.Event()

    async def download_flight(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(flow, "local_setup_memory_ok", lambda: True)
    monkeypatch.setattr(flow, "_run_flight", download_flight)
    try:
        result = await flow.begin_local_setup(onboarding=True)
        await asyncio.wait_for(started.wait(), 2)
        assert result["ok"] and result["in_progress"] and not result.get("existing")
        assert flight.setup_status()["state"] == "starting"
    finally:
        flight._flight.task.cancel()
        await asyncio.gather(flight._flight.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_press_that_cannot_claim_the_operation_says_why_instead_of_reporting_progress(isolated_db, flight, monkeypatch):
    from llm import local_setup_flow as flow, local_setup_operation

    monkeypatch.setattr(flow, "local_setup_memory_ok", lambda: True)
    held = local_setup_operation.try_lock()
    assert held is not None
    try:
        result = await flow.begin_local_setup(onboarding=True)
    finally:
        local_setup_operation.unlock(held)
    assert result["ok"] is False and result["reason"] == "operation_held"
    assert "Quit CORSIA completely" in result["fix"] and not flight._running()
    assert result["ref"].startswith("local-")


@pytest.mark.asyncio
async def test_settings_download_below_memory_floor_returns_shared_refusal(isolated_db, flight, monkeypatch):
    from llm import local_setup_flow as flow
    from llm.local_model import LocalModel
    from llm.local_setup_prepare import NO_LOCAL_MODEL

    monkeypatch.setattr(flow, "local_setup_memory_ok", lambda: False)
    result = await flow.begin_settings_download(LocalModel.__new__(LocalModel))
    assert result == {"ok": False, "reason": "mac_cannot_run", "message": NO_LOCAL_MODEL}
    assert not flight._running()


@pytest.mark.asyncio
async def test_a_settings_press_that_cannot_claim_the_operation_says_why_and_logs_a_reference(isolated_db, flight, monkeypatch):
    from llm import local_setup_flow as flow, local_setup_operation
    from llm.local_model import LocalModel

    monkeypatch.setattr(flow, "local_setup_memory_ok", lambda: True)
    warned = []
    monkeypatch.setattr(flight.log, "warning", lambda message, *args: warned.append(message % args))
    held = local_setup_operation.try_lock()
    assert held is not None
    try:
        result = await flow.begin_settings_download(LocalModel.__new__(LocalModel))
    finally:
        local_setup_operation.unlock(held)
    assert result["ok"] is False and result["reason"] == "operation_held" and not flight._running()
    assert warned == [f"Local setup could not claim its operation reference={result['ref']}"]
