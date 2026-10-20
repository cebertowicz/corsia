"""Terminal setup delivery reads the same persisted flight as recovery and status."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def wake(isolated_db, monkeypatch, tmp_path):
    from llm import local_setup
    from onboarding import local_download_wake

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr("infra.corsia_agent.get_corsia_agent", lambda: "conductor")
    return local_download_wake


def _record(result, *, flight_id="flight-1", downloaded=False, kind="setup", onboarding=True):
    from llm import local_setup_operation as records
    from llm.local_model import LocalModel

    operation = replace(records.Operation.for_model(LocalModel("test/qwen", 1024, 32768), kind, onboarding=onboarding),
                        flight_id=flight_id, result=result, downloaded=downloaded)
    records.write(operation)
    return operation


@pytest.mark.asyncio
async def test_terminal_outcome_finished_before_first_run_render_persists_then_delivers_once(monkeypatch, wake, caplog):
    from llm import local_setup_operation as records

    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", delivered)
    monkeypatch.setattr(wake, "_first_run_rendered", lambda _thread_id: False)
    result = {"ok": False, "cause": "disk_full", "ref": "setup-42", "bytes_to_free": 2048}
    _record(result)

    assert await wake.flush_pending_local_download("conductor") is False
    assert records.read().result == result
    assert records.read().notified is False
    delivered.assert_not_awaited()

    monkeypatch.setattr(wake, "_first_run_rendered", lambda _thread_id: True)
    assert await wake.flush_pending_local_download("conductor") is True
    assert delivered.await_count == 1
    assert delivered.await_args.kwargs["delivery_id"] == "local-download:flight-1:failed"
    notice = delivered.await_args.kwargs["notice"]
    assert "Free 2048 bytes, then retry in Settings › Providers, where Retry is." in notice
    assert '"ref": "setup-42"' in notice
    assert records.read().notified is True
    assert await wake.flush_pending_local_download("conductor") is False
    assert delivered.await_count == 1
    assert not caplog.records


@pytest.mark.asyncio
async def test_failed_delivery_logs_once_and_keeps_the_same_durable_flight(monkeypatch, wake, caplog):
    from llm import local_setup_operation as records

    operation = _record({"ok": False, "cause": "connection", "ref": "start-2"}, flight_id="flight-2", downloaded=True)
    delivered = AsyncMock(side_effect=OSError("delivery unavailable"))
    monkeypatch.setattr("events.delivery.deliver_to_thread", delivered)
    monkeypatch.setattr(wake, "_first_run_rendered", lambda _thread_id: True)

    assert await wake.flush_pending_local_download() is False
    assert records.read() == operation
    assert delivered.await_count == 1
    assert sum("Could not deliver local-download wake local-download:flight-2:start_failed" in row.message for row in caplog.records) == 1
    assert caplog.records[-1].exc_info is not None


@pytest.mark.asyncio
async def test_startup_restores_terminal_state_without_redownloading_and_delivers_once(monkeypatch, wake):
    from llm import local_setup, local_setup_new_omlx as flow, local_setup_operation as records

    _record({"ok": True}, flight_id="restart")
    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", delivered)
    monkeypatch.setattr(wake, "_first_run_rendered", lambda _thread_id: True)
    monkeypatch.setattr(local_setup, "start_flight", lambda *_a, **_k: pytest.fail("terminal flight must not resume"))

    assert await flow.resume_pending_setup() is False
    assert local_setup.setup_status()["state"] == "ready"
    assert await wake.resume_pending_local_download() is None
    assert delivered.await_count == 1
    assert records.read().notified is True
    assert await wake.resume_pending_local_download() is None
    assert delivered.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("result,downloaded,outcome", [
    ({"ok": True}, True, "ready"),
    ({"ok": False, "pre_download": True}, False, "setup_failed"),
    ({"ok": False}, True, "start_failed"),
    ({"ok": False}, False, "failed"),
])
async def test_one_terminal_outcome_feeds_status_rail_and_wake(monkeypatch, wake, result, downloaded, outcome):
    from llm import local_setup
    from onboarding import local_download_state

    result = {**result, "cause": "other", "ref": "local-17"}
    operation = _record(result, downloaded=downloaded)
    local_setup.restore_terminal_flight(operation)
    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", delivered)
    monkeypatch.setattr(wake, "_first_run_rendered", lambda _thread_id: True)
    await local_setup._notify_terminal_setup_download(result)

    assert local_setup.setup_status()["state"] == outcome
    assert local_download_state.current()["state"] == outcome
    assert local_download_state.current()["ref"] == "local-17"
    assert delivered.await_args.kwargs["delivery_id"] == f"local-download:flight-1:{outcome}"
    if outcome != "ready":
        assert "local-17" in delivered.await_args.kwargs["notice"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,onboarding,result", [
    ("setup", True, {"ok": True, "downloading": True}),
    ("setup", True, {"ok": True, "cancelled": True}),
    ("setup", False, {"ok": True}),
    ("settings", True, {"ok": False}),
])
async def test_nonterminal_cancelled_and_nononboarding_work_never_wake(monkeypatch, wake, kind, onboarding, result):
    from llm import local_setup

    local_setup.restore_terminal_flight(_record(result, kind=kind, onboarding=onboarding))
    delivered = AsyncMock()
    monkeypatch.setattr("events.delivery.deliver_to_thread", delivered)
    await local_setup._notify_terminal_setup_download(result)
    assert await wake.flush_pending_local_download() is False
    delivered.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_reference_matches_the_rail_and_log(monkeypatch, wake, caplog):
    from llm import local_setup, local_setup_new_omlx as flow
    from onboarding import local_download_state

    async def failed_setup(*_args, **_kwargs):
        raise RuntimeError("setup refused")

    monkeypatch.setattr(flow, "_setup_new_omlx", failed_setup)
    monkeypatch.setattr(local_setup, "_emit", lambda *_args, **_kwargs: None)
    assert local_setup._claim_operation("setup", onboarding=True)
    result = await flow._run_flight()
    rail = local_download_state.current()
    assert rail["state"] == "setup_failed"
    assert rail["ref"] == result["ref"]
    assert f"reference={result['ref']}" in caplog.text


@pytest.mark.parametrize("error", [ImportError("port unavailable"), RuntimeError("connection dropped")])
def test_flight_failure_cause_does_not_match_error_text(error):
    from llm.local_setup_results import failure_cause

    assert failure_cause(error) == "other"


@pytest.mark.parametrize("cause", ["connection", "disk_full", "memory", "other"])
def test_flight_causes_reach_rail_and_wake_identically(cause):
    from onboarding import local_download_state, local_download_wake

    rail = local_download_state.normalize({"state": "failed", "cause": cause})
    payload = local_download_wake._payload("failed", model=None, cause=cause, ref=None, bytes_to_free=None)
    assert rail["cause"] == payload["cause"] == cause


def test_wake_does_not_classify_start_path_other_omlx_as_a_flight_cause():
    from onboarding import local_download_wake

    payload = local_download_wake._payload("failed", model=None, cause="other_omlx", ref=None, bytes_to_free=None)
    assert payload["cause"] == "other"


@pytest.mark.parametrize("cause", ["disk_full", "connection", "memory", "own_omlx_upgrade_required", "other"])
def test_wake_and_status_route_share_one_fix(wake, cause):
    from llm import local_setup

    payload = wake._payload("failed", model=None, cause=cause, ref="local-9", bytes_to_free=None)
    local_setup._flight.result = {"ok": False, "cause": cause, "ref": "local-9"}
    status = local_setup.setup_status()

    assert status["state"] == "failed"
    assert status["fix"] == payload["fix"]
    assert status["ref"] == payload["ref"] == "local-9"


def test_status_carries_a_fix_only_for_a_failed_setup(wake):
    from llm import local_setup

    assert local_setup.setup_status()["fix"] is None
    local_setup._flight.result = {"ok": True}
    assert local_setup.setup_status()["fix"] is None
