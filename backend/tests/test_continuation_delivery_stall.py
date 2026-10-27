"""Continuation delivery remains live when a Codex steer does not answer."""

import asyncio

import pytest

from tests.test_codex_inbox_steer import _clear_live, _install_live, _seed_live_turn


@pytest.mark.asyncio
async def test_event_send_returns_while_codex_steer_waits(isolated_db):
    from events.delivery import deliver_to_thread

    thread_id = "stalled-steer"
    identity = _seed_live_turn(thread_id)
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    class Session:
        async def steer(self, text, *, on_write_observed, on_drain_confirmed, input_context=None):
            entered.set()
            await release.wait()
            finished.set()
            return False

    _install_live(thread_id, identity, Session())
    task = asyncio.create_task(deliver_to_thread(thread_id, "test.event", "test wake"))
    try:
        done, _ = await asyncio.wait({task}, timeout=3)
        assert task in done, "event send waited for the Codex steer reply"
        assert task.result() is True
        await asyncio.wait_for(entered.wait(), 3)
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _clear_live(thread_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("write_observed", [False, True])
async def test_timed_out_steer_releases_live_lock_and_preserves_fifo(
    isolated_db, monkeypatch, caplog, write_observed,
):
    from events.delivery import _event_idempotency_key, deliver_to_thread
    from infra import send_post_admission
    from subagents.codex_runtime_state import _LIVE
    from tests.test_codex_inbox_steer import _input_row
    from ws.ingress import background_tasks

    thread_id = "timed-out-steer"
    identity = _seed_live_turn(thread_id)
    entered = asyncio.Event()

    class Session:
        async def steer(self, text, *, on_write_observed, on_drain_confirmed, input_context=None):
            if write_observed:
                on_write_observed()
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(send_post_admission, "EVENT_BOUNDARY_TIMEOUT_SECONDS", 0.1)
    _install_live(thread_id, identity, Session())
    try:
        assert await deliver_to_thread(thread_id, "test.event", "timeout wake")
        await asyncio.wait_for(entered.wait(), 5)
        tasks = tuple(background_tasks)
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        async def take_lock():
            async with _LIVE.lock(thread_id):
                return True
        assert await asyncio.wait_for(take_lock(), 1)
        row = _input_row(_event_idempotency_key("test.event", "timeout wake", None))
        assert row["delivery_state"] == ("ambiguous" if write_observed else "queued")
        assert "safe boundary timed out" in caplog.text
    finally:
        _clear_live(thread_id)
