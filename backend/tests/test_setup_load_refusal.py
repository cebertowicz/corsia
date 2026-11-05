"""The setup flight reports a refused load and retries its recorded model."""

from unittest.mock import AsyncMock

import pytest

from llm import local_setup, local_setup_flow
from llm.local_model import LocalModel
from llm.local_setup_results import setup_fix, RETRY_IN_TERMINAL
from onboarding.local_download_state import normalize

MODEL = LocalModel("owner/example", 1000, 32768)


@pytest.mark.parametrize("cause", ["other", "connection"])
def test_unrelated_failure_does_not_expose_message_in_setup(cause):
    from llm.local_sources import _project_local_flight

    row = {}
    _project_local_flight(row, {"state": "failed", "cause": cause, "message": "private exception", "fix": "Try again"})
    assert "private exception" not in str(row)
    assert "detail" not in row


@pytest.mark.asyncio
@pytest.mark.parametrize("display", ["Friendly Model", None])
async def test_no_room_uses_catalog_display_name_or_fallback(display, monkeypatch):
    from llm.local_setup_results import LoadRefused

    monkeypatch.setattr(local_setup_flow, "keep_loaded", AsyncMock(return_value={
        "status": "refused", "verdict": "no_room", "line": "Not loaded: exact line", "neighbour_id": "other",
    }))
    row = {"litellm_model": "omlx@server/other", "display_name": display} if display else None
    monkeypatch.setattr("llm.catalog.get_model_by_litellm", lambda _identity: row)
    with pytest.raises(LoadRefused) as caught:
        await local_setup_flow._check_load("server", MODEL)
    exc = caught.value
    fix = setup_fix("load_refused", verdict=exc.verdict, neighbour=exc.neighbour)
    assert exc.line == "Not loaded: exact line"
    if display:
        assert fix == "CORSIA couldn’t load the model while Friendly Model is loaded. Unload it in oMLX or quit the app using it, then press Retry."
    else:
        assert fix == "CORSIA couldn’t load the model while another model is loaded. Unload it in oMLX or quit the app using it, then press Retry."
    assert setup_fix("load_refused", retry=RETRY_IN_TERMINAL, verdict="no_room").endswith(
        "Unload it in oMLX or quit the app using it, then run `corsia local setup` again."
    )


@pytest.mark.asyncio
async def test_missing_recorded_model_returns_to_download_state(monkeypatch, tmp_path):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup_flow.prepare, "join", AsyncMock())
    monkeypatch.setattr("llm.local_sources.local_server_for_install", AsyncMock(return_value=("server", 8210, True)))
    monkeypatch.setattr("llm.local_model.set_up_model", lambda: None)
    monkeypatch.setattr(local_setup_flow, "keep_loaded", AsyncMock())
    result = await local_setup_flow._setup_new_omlx(lambda *_a, **_k: None, [], MODEL, retry_after_load_failure=True)
    assert result["state"] == "stopped"
    assert local_setup.selected_model() is None
    local_setup_flow.keep_loaded.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,line", [("no_room", "Not loaded: needs 12 GB, 8 GB free for a model now"), ("no_memory", None)])
async def test_refusal_is_a_referenced_start_failure_and_projected_fix(verdict, line, monkeypatch, tmp_path):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup, "_notify_models_changed", lambda: None)
    emitted = []
    monkeypatch.setattr(local_setup, "_emit", lambda action, **_k: emitted.append(action))
    monkeypatch.setattr(local_setup, "_notify_terminal_setup_download", AsyncMock())
    monkeypatch.setattr(local_setup_flow.prepare, "join", AsyncMock())
    monkeypatch.setattr("llm.local_sources.local_server_for_install", AsyncMock(return_value=("server", 8210, True)))
    monkeypatch.setattr("llm.local_model.set_up_model", lambda: MODEL)
    monkeypatch.setattr("llm.local_keep_loaded._model_rows", AsyncMock(return_value=[{"id": MODEL.model}]))
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", AsyncMock(return_value=[]))
    monkeypatch.setattr(local_setup_flow, "keep_loaded", AsyncMock(return_value={"status": "refused", "verdict": verdict, "line": line}))
    assert local_setup._claim_operation("setup", onboarding=True)

    result = await local_setup_flow._run_flight(model=MODEL, retry_after_load_failure=True)
    status = local_setup.setup_status()
    projected = normalize(status)

    assert not result["ok"] and status["state"] == "start_failed"
    assert status["cause"] == "load_refused" and status["ref"].startswith("local-")
    assert projected["fix"] == setup_fix("load_refused", verdict=verdict)
    assert projected.get("catalog_line") == line
    local_setup_flow.keep_loaded.assert_awaited_once_with("server", MODEL, force_pin=True)
    assert "run `corsia local setup` again" in setup_fix("load_refused", retry=RETRY_IN_TERMINAL)

    local_setup_flow.keep_loaded.return_value = {"status": "loaded"}
    started = await local_setup_flow.begin_local_setup(onboarding=True)
    assert started["in_progress"]
    retry = await local_setup.flight_task()
    assert retry["ok"] and local_setup.setup_status()["state"] == "ready"
    assert emitted[-2:] == ["started", "completed"]
    assert local_setup_flow.keep_loaded.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,cause", [
    ({"status": "refused", "verdict": "no_memory", "line": "Not loaded: needs 12 GB, 8 GB free for a model now"}, "load_refused"),
    ({"status": "failed", "detail": "private upstream detail"}, "load_failed"),
])
async def test_first_download_failure_is_not_ready(outcome, cause, monkeypatch, tmp_path, caplog):
    from types import SimpleNamespace

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup, "_notify_models_changed", lambda: None)
    monkeypatch.setattr(local_setup, "_emit", lambda *_a, **_k: None)
    monkeypatch.setattr(local_setup, "_download", AsyncMock())
    monkeypatch.setattr(local_setup, "_notify_terminal_setup_download", AsyncMock())
    monkeypatch.setattr(local_setup_flow.prepare, "join", AsyncMock())
    monkeypatch.setattr("llm.local_sources.local_server_for_install", AsyncMock(return_value=("server", 8210, False)))
    monkeypatch.setattr(local_setup_flow, "apply_optimal_settings", AsyncMock(return_value=SimpleNamespace(status="applied", writer_attempted=True)))
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(local_setup_flow, "keep_loaded", AsyncMock(return_value=outcome))
    assert local_setup._claim_operation("setup", MODEL, onboarding=True)

    result = await local_setup_flow._run_flight(model=MODEL)
    assert not result["ok"] and local_setup.setup_status()["state"] == "start_failed"
    status = local_setup.setup_status()
    assert status["cause"] == cause and status["ref"].startswith("local-")
    if cause == "load_failed":
        assert status["fix"] == setup_fix("load_failed") and not status["message"]
        assert "private upstream detail" in caplog.text and status["ref"] in caplog.text
        assert "private upstream detail" not in str(normalize(status))
        monkeypatch.setattr("llm.local_sources.local_server_for_install", AsyncMock(return_value=("server", 8210, True)))
        monkeypatch.setattr("llm.local_model.set_up_model", lambda: MODEL)
        monkeypatch.setattr("llm.local_keep_loaded._model_rows", AsyncMock(return_value=[{"id": MODEL.model}]))
        monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", AsyncMock(return_value=[]))
        started = await local_setup_flow.begin_local_setup(onboarding=True)
        assert started["in_progress"]
        retry = await local_setup.flight_task()
        assert not retry["ok"] and local_setup.setup_status()["cause"] == "load_failed"
        monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", AsyncMock(return_value=None))
        assert (await local_setup_flow.begin_local_setup(onboarding=True))["in_progress"]
        assert not (await local_setup.flight_task())["ok"]
        assert local_setup.setup_status()["cause"] == "load_failed"
        assert "Run `corsia local setup` again" in setup_fix("load_failed", retry=RETRY_IN_TERMINAL)


@pytest.mark.asyncio
@pytest.mark.parametrize("from_outcome", [False, True])
async def test_setup_logs_original_load_exception_with_reference(from_outcome, monkeypatch, tmp_path, caplog):
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

    async def fail(*_args, **_kwargs):
        try:
            raise TypeError("private diagnostic")
        except TypeError as exc:
            if from_outcome:
                return {"status": "failed", "exception": exc}
            raise

    monkeypatch.setattr(local_setup_flow, "keep_loaded", fail)
    assert local_setup._claim_operation("setup", onboarding=True)
    result = await local_setup_flow._run_flight(model=MODEL, retry_after_load_failure=True)
    assert result["cause"] == "load_failed" and result["ref"].startswith("local-")
    assert result["ref"] in caplog.text and "TypeError: private diagnostic" in caplog.text
    assert any(record.exc_info for record in caplog.records if result["ref"] in record.message)
    assert "private diagnostic" not in str(normalize(local_setup.setup_status()))


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
    keep = AsyncMock(return_value={"status": "loaded"})
    monkeypatch.setattr(local_setup_flow, "keep_loaded", keep)
    report = lambda _stage, **_kwargs: None

    assert (await local_setup_flow._setup_new_omlx(report, [], MODEL, retry_after_load_failure=True))["ok"]
    keep.assert_awaited_once_with("server", MODEL, force_pin=True)
    loaded.return_value = [{"identifier": MODEL.model}]
    keep.reset_mock()
    assert (await local_setup_flow._setup_new_omlx(report, [], MODEL, retry_after_load_failure=True))["ok"]
    keep.assert_awaited_once_with("server", MODEL, force_pin=True)


def test_cli_no_line_uses_terminal_retry_copy(monkeypatch, capsys):
    import cli_local

    monkeypatch.setattr(cli_local, "request_json", lambda *_a: {
        "state": "start_failed", "cause": "load_refused", "message": "", "ref": "local-42",
    })
    assert cli_local._follow_server_setup("test", "corsia local setup") == 2
    output = capsys.readouterr().out
    assert "run `corsia local setup` again" in output
    assert "press Retry" not in output and "local-42" in output


@pytest.mark.parametrize("cause", ["load_refused", "load_failed"])
def test_restored_failure_projects_through_ready_source(cause, monkeypatch, tmp_path):
    from dataclasses import replace
    from llm import local_setup_operation, local_sources

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    line = "Not loaded: needs 12 GB, 8 GB free for a model now"
    operation = replace(local_setup_operation.Operation.for_model(MODEL, "setup", onboarding=True),
                        result={"ok": False, "cause": cause, "catalog_line": line if cause == "load_refused" else None, "verdict": "no_memory", "ref": "local-42"},
                        downloaded=True)
    local_setup_operation.write(operation)
    local_setup.restore_terminal_flight(local_setup_operation.read())
    monkeypatch.setattr(local_sources, "host_never_runs_local", lambda: False)
    monkeypatch.setattr(local_sources, "local_memory_ok", lambda: True)
    monkeypatch.setattr("llm.local_setup_prepare.known_model", lambda: MODEL)
    monkeypatch.setattr(local_sources, "local_omlx_servers", lambda _rows: [{"id": "server", "status": "online"}])
    monkeypatch.setattr(local_sources, "corsia_omlx_server", lambda: {"id": "server", "status": "online"})
    monkeypatch.setattr("llm.catalog.get_model_by_litellm", lambda identity: {"litellm_model": identity, "available": True, "enabled": True})
    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _server: {"another-model"})

    row = local_sources._omlx_state(({"omlx@server"}, set()))
    assert row["local_state"] == "start_failed"
    assert row["fix"] == setup_fix(cause, verdict="no_memory")
    assert row.get("detail") == (line if cause == "load_refused" else None)
    assert row["setup_error_reference"] == "local-42"

    # A GET must not rewrite the persisted failure, even if cached observation changes.
    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _server: {MODEL.model})
    assert local_sources._omlx_state(({"omlx@server"}, set()))["local_state"] == "start_failed"
    assert local_setup_operation.read().result["cause"] == cause


def test_retry_after_refusal_projects_starting_until_loaded(monkeypatch, tmp_path):
    from dataclasses import replace
    from llm import local_setup_operation, local_sources

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    operation = replace(local_setup_operation.Operation.for_model(MODEL, "setup", onboarding=True),
                        result={"ok": False, "cause": "load_refused", "catalog_line": "Not loaded: needs 19 GB, 13 GB free for a model now", "verdict": "no_memory", "ref": "local-42"},
                        downloaded=True)
    local_setup_operation.write(operation)
    local_setup.restore_terminal_flight(local_setup_operation.read())
    monkeypatch.setattr(local_sources, "host_never_runs_local", lambda: False)
    monkeypatch.setattr(local_sources, "local_memory_ok", lambda: True)
    monkeypatch.setattr("llm.local_setup_prepare.known_model", lambda: MODEL)
    monkeypatch.setattr(local_sources, "local_omlx_servers", lambda _rows: [{"id": "server", "status": "online"}])
    monkeypatch.setattr(local_sources, "corsia_omlx_server", lambda: {"id": "server", "status": "online"})
    monkeypatch.setattr("llm.catalog.get_model_by_litellm", lambda identity: {"litellm_model": identity, "available": True, "enabled": True})
    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _server: set())
    states = ({"omlx@server"}, set())
    assert local_sources._omlx_state(states)["local_state"] == "start_failed"

    # Retry: the new flight clears the refusal and loads the model on disk. The row must never read
    # as an idle, undownloaded model ("stopped" is what the Download offer is built on).
    class Running:
        def done(self):
            return False

    local_setup._flight.result, local_setup._flight.task = None, Running()
    assert local_sources._omlx_state(states)["local_state"] == "starting"

    local_setup._flight.task = None
    local_setup._flight.result = {"ok": True}
    assert local_sources._omlx_state(states)["local_state"] == "stopped"
