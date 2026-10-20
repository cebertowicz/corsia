"""State and upgrade regressions across the setup flight's consumers."""

import asyncio
from dataclasses import replace
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

    operation = replace(records.Operation.for_model(None, "setup", onboarding=True), result={"ok": False})
    records.write(operation)
    monkeypatch.setattr("llm.local_model.backfill_legacy_record", lambda: None)
    monkeypatch.setattr("llm.local_setup_new_omlx.resume_pending_setup", AsyncMock(return_value=False))
    monkeypatch.setattr("onboarding.local_download_wake.resume_pending_local_download", AsyncMock(side_effect=RuntimeError("notice failed")))
    await app_startup_runtime.resume_local_setup()
    status = flight.setup_status()
    assert status["state"] == "failed"
    assert status["ref"].startswith("local-")
    assert local_download_state.current()["ref"] == status["ref"]
    assert records.read().result["ref"] == status["ref"]
    assert any(row.exc_info and f"reference={status['ref']}" in row.message for row in caplog.records)


@pytest.mark.asyncio
async def test_existing_registered_local_model_is_visible_and_setup_and_prepare_do_not_modify_it(isolated_db, flight, monkeypatch):
    from llm import local_setup_new_omlx as flow, local_setup_prepare as prepare, local_sources
    from llm import network_discovery as discovery
    from test_managed_omlx_ownership import _model_row

    sid = discovery.upsert_server(provider="omlx", instance_id="127.0.0.1:8000", instance_name="Existing oMLX",
                                  host="127.0.0.1", port=8000, status="online", kind="managed")
    _model_row("old-local-model", f"omlx@{sid}/existing/model", "existing/model")
    monkeypatch.setattr(prepare, "_prepare", prepare._Prepare())
    monkeypatch.setattr(prepare, "start_managed_omlx", AsyncMock(side_effect=AssertionError("must not start oMLX")))
    monkeypatch.setattr(flow, "start_managed_omlx", AsyncMock(side_effect=AssertionError("must not start oMLX")))
    monkeypatch.setattr(prepare, "local_memory_ok", lambda: True)
    monkeypatch.setattr(flow, "local_memory_ok", lambda: True)
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
async def test_failed_boot_retains_identity_until_the_supervised_process_exits(flight, monkeypatch):
    from llm.providers import omlx_supervisor as supervisor

    supervisor._write_pid("boot-failed", 107)
    monkeypatch.setattr(supervisor, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(supervisor, "_process_exited", lambda _pid: False)
    monkeypatch.setattr(supervisor.os, "kill", lambda *_args: None)
    monkeypatch.setattr(supervisor, "_tail_log", lambda *_a, **_k: "")
    monkeypatch.setattr(supervisor, "_port_owner_pid", lambda *_args: None)
    monkeypatch.setattr(supervisor, "_spawn_process", lambda *_args: pytest.fail("must not lose a live spawn"))
    assert supervisor._failed_boot_result("boot-failed", "127.0.0.1", 8210, 107)["ok"] is False
    assert supervisor.is_corsia_process(107)
    assert (await supervisor.start_server({"server_id": "boot-failed", "host": "127.0.0.1", "port": 8210}))["ok"] is False
    assert supervisor.is_corsia_process(107)


def test_unreleased_boolean_choice_shape_does_not_apply(flight):
    from llm import local_setup_foreign as foreign

    foreign.foreign_omlx_choice_path().write_text('{"stop": true, "pid": 101}')
    assert foreign.foreign_omlx_answer() == "none"
