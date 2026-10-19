"""A setup download stays queued until the first-run wake can receive it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def wake(isolated_db, monkeypatch):
    from onboarding import local_download_wake

    monkeypatch.setattr("infra.corsia_agent.get_corsia_agent", lambda: "conductor")
    return local_download_wake


@pytest.mark.asyncio
async def test_terminal_outcome_finished_before_first_run_render_persists_then_delivers_once(monkeypatch, wake):
    from infra import setup_progress

    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", delivered)
    monkeypatch.setattr(wake, "_first_run_rendered", lambda _thread_id: False)

    queued = await wake.notify_terminal_setup_download(
        "flight-1", "failed", cause="disk_full", ref="setup-42", bytes_to_free=2048,
    )

    assert queued is False
    assert setup_progress.pending_local_download_wakes() == {
        "local-download:flight-1:failed": {
            "outcome": "failed", "cause": "disk_full", "fix": "Free 2048 bytes, then retry in Settings.",
            "bytes_to_free": 2048,
        },
    }
    delivered.assert_not_awaited()

    monkeypatch.setattr(wake, "_first_run_rendered", lambda _thread_id: True)
    assert await wake.flush_pending_local_download("conductor") is True

    assert delivered.await_count == 1
    assert delivered.await_args.kwargs["delivery_id"] == "local-download:flight-1:failed"
    assert setup_progress.pending_local_download_wakes() == {}


@pytest.mark.asyncio
async def test_failed_delivery_after_render_logs_once_and_keeps_the_durable_row(monkeypatch, wake, caplog):
    from infra import setup_progress

    delivered = AsyncMock(side_effect=OSError("delivery unavailable"))
    monkeypatch.setattr("events.delivery.deliver_to_thread", delivered)
    monkeypatch.setattr(wake, "_first_run_rendered", lambda _thread_id: True)

    assert await wake.notify_terminal_setup_download("flight-2", "start_failed", cause="connection", ref="start-2") is False

    assert delivered.await_count == 1
    assert "local-download:flight-2:start_failed" in setup_progress.pending_local_download_wakes()
    assert sum("Could not deliver local-download wake local-download:flight-2:start_failed" in record.message for record in caplog.records) == 1


@pytest.mark.asyncio
async def test_startup_resume_delivers_a_persisted_row_and_starts_nothing_when_empty(monkeypatch, wake):
    from infra import setup_progress

    setup_progress.record_local_download_wake(
        "local-download:flight-restart:failed",
        {"outcome": "failed", "cause": "connection", "fix": "Check the connection, then retry in Settings."},
    )
    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", delivered)
    monkeypatch.setattr(wake, "_first_run_rendered", lambda _thread_id: True)

    assert await wake.resume_pending_local_download() is None

    assert delivered.await_count == 1
    assert setup_progress.pending_local_download_wakes() == {}
    assert await wake.resume_pending_local_download() is None
    assert wake.retry_task() is None


@pytest.mark.asyncio
async def test_local_setup_caller_passes_its_ref_keyword_to_the_durable_hook(monkeypatch):
    from llm import local_setup
    from llm.local_model import LocalModel

    called = AsyncMock(return_value=False)
    monkeypatch.setattr("onboarding.local_download_wake.notify_terminal_setup_download", called)
    monkeypatch.setattr(local_setup.local_setup_operation, "read", lambda: SimpleNamespace(task_id="flight-3"))
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight(
        kind="setup", model=LocalModel("qwen", 1024, 2048), downloaded=True,
    ))

    await local_setup._notify_terminal_setup_download({"ok": False, "cause": "connection", "ref": "local-17"})

    assert called.await_args.args == ("flight-3", "start_failed")
    assert called.await_args.kwargs == {
        "model": "qwen", "cause": "connection", "ref": "local-17", "bytes_to_free": None,
    }

    called.reset_mock()
    local_setup._flight.downloaded = False
    await local_setup._notify_terminal_setup_download({"ok": False, "cause": "disk_full", "ref": "local-18"})
    assert called.await_args.args == ("flight-3", "failed")
