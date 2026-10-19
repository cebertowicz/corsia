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
    monkeypatch.setattr(local_download_wake, "_RETRY_DELAYS_SECONDS", (0,))
    monkeypatch.setattr(local_download_wake, "_BACKGROUND_RETRY_DELAYS_SECONDS", (0, 0, 0))
    return local_download_wake


@pytest.mark.asyncio
async def test_terminal_outcome_finished_before_first_run_render_persists_then_delivers_once(monkeypatch, wake):
    from infra import setup_progress

    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", delivered)
    monkeypatch.setattr(wake, "_first_run_rendered", lambda _thread_id: False)

    queued = await wake.notify_terminal_setup_download(
        "flight-1", "failed", cause="DiskSpaceError", ref="setup-42", bytes_to_free=2048,
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
async def test_failed_delivery_retries_beyond_the_initial_call_then_logs_a_durable_terminal_error(monkeypatch, wake, caplog):
    from infra import setup_progress

    delivered = AsyncMock(side_effect=OSError("delivery unavailable"))
    monkeypatch.setattr("events.delivery.deliver_to_thread", delivered)
    monkeypatch.setattr(wake, "_first_run_rendered", lambda _thread_id: True)

    assert await wake.notify_terminal_setup_download("flight-2", "start_failed", cause="NetworkError", ref="start-2") is False
    retry = wake.retry_task()
    assert retry is not None
    await asyncio.wait_for(retry, timeout=1)

    # The initial two attempts plus three bounded retry passes prove retries outlive the call.
    assert delivered.await_count == 8
    assert "local-download:flight-2:start_failed" in setup_progress.pending_local_download_wakes()
    assert any(
        "retry exhausted; durable row remains: local-download:flight-2:start_failed" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_startup_resume_delivers_a_persisted_row_and_starts_nothing_when_empty(monkeypatch, wake):
    from infra import setup_progress

    setup_progress.record_local_download_wake(
        "local-download:flight-restart:failed",
        {"outcome": "failed", "cause": "connection", "fix": "Check the connection, then retry in Settings."},
    )
    delivered = AsyncMock(side_effect=[OSError("temporarily unavailable"), OSError("still unavailable"), True])
    monkeypatch.setattr("events.delivery.deliver_to_thread", delivered)
    monkeypatch.setattr(wake, "_first_run_rendered", lambda _thread_id: True)

    retry = await wake.resume_pending_local_download()
    assert retry is not None
    await asyncio.wait_for(retry, timeout=1)

    assert delivered.await_count == 3
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
        kind="setup", model=LocalModel("qwen", 1024, 2048),
    ))

    await local_setup._notify_terminal_setup_download({"ok": False, "cause": "NetworkError", "ref": "local-17"})

    assert called.await_args.args == ("flight-3", "failed")
    assert called.await_args.kwargs == {
        "model": "qwen", "cause": "NetworkError", "ref": "local-17", "bytes_to_free": None,
    }
