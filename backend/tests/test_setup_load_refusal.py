"""The setup flight reports a refused load and retries its recorded model."""

from unittest.mock import AsyncMock

import pytest

from llm import local_setup, local_setup_flow
from llm.local_model import LocalModel
from llm.local_setup_results import setup_fix, RETRY_IN_TERMINAL
from onboarding.local_download_state import normalize

MODEL = LocalModel("owner/example", 1000, 32768)


@pytest.mark.asyncio
@pytest.mark.parametrize("line", ["Not loaded: needs 12 GB, 8 GB free for a model now", None])
async def test_refusal_is_a_referenced_start_failure_and_projected_fix(line, monkeypatch, tmp_path):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup, "_notify_models_changed", lambda: None)
    monkeypatch.setattr(local_setup, "_emit", lambda *_a, **_k: None)
    monkeypatch.setattr(local_setup, "_notify_terminal_setup_download", AsyncMock())
    monkeypatch.setattr(local_setup_flow.prepare, "join", AsyncMock())
    monkeypatch.setattr("llm.local_sources.local_server_for_install", AsyncMock(return_value=("server", 8210, True)))
    monkeypatch.setattr("llm.local_model.set_up_model", lambda: MODEL)
    monkeypatch.setattr("llm.local_keep_loaded._model_rows", AsyncMock(return_value=[{"id": MODEL.model}]))
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", AsyncMock(return_value=[]))
    monkeypatch.setattr(local_setup_flow, "keep_loaded", AsyncMock(return_value={"refused": True, "line": line}))
    assert local_setup._claim_operation("setup", onboarding=True)

    result = await local_setup_flow._run_flight(model=MODEL)
    status = local_setup.setup_status()
    projected = normalize(status)

    assert not result["ok"] and status["state"] == "start_failed"
    assert status["cause"] == "load_refused" and status["ref"].startswith("local-")
    assert projected["fix"] == (line or setup_fix("load_refused"))
    local_setup_flow.keep_loaded.assert_awaited_once_with("server", MODEL, force_pin=True)
    assert "run `corsia local setup` again" in setup_fix("load_refused", retry=RETRY_IN_TERMINAL)

    local_setup_flow.keep_loaded.return_value = None
    started = await local_setup_flow.begin_local_setup(onboarding=True)
    assert started["in_progress"]
    retry = await local_setup.flight_task()
    assert retry["ok"] and local_setup.setup_status()["state"] == "ready"
    assert local_setup_flow.keep_loaded.await_count == 2


@pytest.mark.asyncio
async def test_retry_loads_recorded_model_and_loaded_model_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup, "_notify_models_changed", lambda: None)
    monkeypatch.setattr(local_setup_flow.prepare, "join", AsyncMock())
    monkeypatch.setattr("llm.local_sources.local_server_for_install", AsyncMock(return_value=("server", 8210, True)))
    monkeypatch.setattr("llm.local_model.set_up_model", lambda: MODEL)
    monkeypatch.setattr("llm.local_keep_loaded._model_rows", AsyncMock(return_value=[{"id": MODEL.model}]))
    loaded = AsyncMock(return_value=[])
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", loaded)
    keep = AsyncMock(return_value=None)
    monkeypatch.setattr(local_setup_flow, "keep_loaded", keep)
    report = lambda _stage, **_kwargs: None

    assert (await local_setup_flow._setup_new_omlx(report, [], MODEL))["ok"]
    keep.assert_awaited_once_with("server", MODEL, force_pin=True)
    loaded.return_value = [{"identifier": MODEL.model}]
    keep.reset_mock()
    assert (await local_setup_flow._setup_new_omlx(report, [], MODEL))["existing"]
    keep.assert_not_awaited()


def test_restored_refusal_projects_through_ready_source(monkeypatch, tmp_path):
    from dataclasses import replace
    from llm import local_setup_operation, local_sources

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    line = "Not loaded: needs 12 GB, 8 GB free for a model now"
    operation = replace(local_setup_operation.Operation.for_model(MODEL, "setup", onboarding=True),
                        result={"ok": False, "cause": "load_refused", "message": line, "ref": "local-42"},
                        downloaded=True)
    local_setup_operation.write(operation)
    local_setup.restore_terminal_flight(local_setup_operation.read())
    monkeypatch.setattr(local_sources, "host_never_runs_local", lambda: False)
    monkeypatch.setattr(local_sources, "local_memory_ok", lambda: True)
    monkeypatch.setattr("llm.local_setup_prepare.known_model", lambda: MODEL)
    monkeypatch.setattr(local_sources, "local_omlx_servers", lambda _rows: [{"id": "server", "status": "online"}])

    row = local_sources._omlx_state(({"omlx@server"}, set()))
    assert row["local_state"] == "start_failed"
    assert row["fix"] == line and row["setup_error_reference"] == "local-42"

    local_setup._flight.result = {"ok": True}
    assert local_sources._omlx_state(({"omlx@server"}, set()))["local_state"] == "stopped"
