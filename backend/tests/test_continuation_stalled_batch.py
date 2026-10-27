"""Slow deliveries are diagnosed without cancelling healthy starts."""

import asyncio

import pytest
from loguru import logger

from automation.continuation import loop, runner
from automation.continuation.models import TriggerRequest
from infra.database import get_db
from infra.failed_wake_surface import WAKE_FAILED_REASON
from tests.event_routing_helpers import mk_session


@pytest.mark.asyncio
async def test_slow_delivery_logs_stack_then_settles_delivered(isolated_db, monkeypatch):
    from infra.db_async import run_in_db

    threads = [await run_in_db(mk_session, f"sweep-{i}") for i in range(3)]
    db = get_db()
    release = asyncio.Event()
    warned = asyncio.Event()
    calls, warnings = [], []

    class Source:
        kind = "t1_watchdog"

        def due(self, now, *, db):
            return [TriggerRequest(self.kind, thread, "scope", thread, "one", reason="watchdog_due")
                    for thread in threads]

        def settle(self, trigger, outcome, reason, *, db):
            return None

    async def slow_provider_start():
        await release.wait()

    async def deliver(thread, event, summary, details):
        calls.append(thread)
        if len(calls) == 1:
            await slow_provider_start()
        return True

    def capture(message):
        if "slow delivery" in str(message):
            warnings.append(str(message))
            warned.set()

    monkeypatch.setattr(loop, "CONTINUATION_SWEEP_INTERVAL", 0.02)
    sink = logger.add(capture, level="WARNING")
    task = asyncio.create_task(runner.sweep(
        "2026-10-27T03:00:00+00:00", db=db, sources=[Source()], deliver=deliver,
    ))
    try:
        await asyncio.wait_for(warned.wait(), 3)
        assert not task.done()
        assert len(warnings) == 1
        assert "slow_provider_start" in warnings[0] and "await release.wait()" in warnings[0]
        assert "stack:" in warnings[0] and calls[0] in warnings[0]
        assert db.execute("SELECT state FROM continuation_triggers WHERE thread_id=?", (calls[0],)).fetchone()[0] == "dispatched"
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert set(result["woken"]) == set(threads) and result["errored"] == []
        assert len(warnings) == 1
        assert {row[0] for row in db.execute("SELECT state FROM continuation_triggers")} == {"delivered"}
        assert db.execute("SELECT count(*) FROM dead_letter_events WHERE reason=?", (WAKE_FAILED_REASON,)).fetchone()[0] == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        logger.remove(sink)
        db.close()
