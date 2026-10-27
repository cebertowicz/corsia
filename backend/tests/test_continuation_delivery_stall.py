"""Continuation delivery remains live when a Codex steer does not answer."""

import asyncio

import pytest

from tests.test_codex_inbox_steer import _clear_live, _install_live, _seed_live_turn


@pytest.mark.asyncio
async def test_event_send_returns_while_codex_steer_waits(isolated_db):
    from events.delivery import deliver_to_thread
    from infra.db_async import run_in_db
    from ws.ingress import background_tasks

    thread_id = "stalled-steer"
    identity = await run_in_db(_seed_live_turn, thread_id)
    entered = asyncio.Event()
    release = asyncio.Event()

    class Session:
        async def steer(self, text, *, on_write_observed, on_drain_confirmed, input_context=None):
            entered.set()
            await release.wait()
            return False

    _install_live(thread_id, identity, Session())
    previous = set(background_tasks)
    task = asyncio.create_task(deliver_to_thread(thread_id, "test.event", "test wake"))
    try:
        done, _ = await asyncio.wait({task}, timeout=3)
        assert task in done, "event send waited for the Codex steer reply"
        assert task.result() is True
        await asyncio.wait_for(entered.wait(), 3)
    finally:
        release.set()
        await asyncio.gather(*(set(background_tasks) - previous), return_exceptions=True)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _clear_live(thread_id)
