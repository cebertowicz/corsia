"""A cancellation-resistant delivery cannot hold up other continuation wakes."""

import asyncio
import json

import pytest

from automation.continuation import execution, runner
from automation.continuation.models import TriggerRequest
from events.delivery import _event_idempotency_key, enqueue_event_wake
from infra.database import get_db
from infra.failed_wake_surface import WAKE_FAILED_REASON
from tests.event_routing_helpers import mk_session


@pytest.mark.asyncio
async def test_stuck_delivery_is_reported_and_other_wakes_continue(isolated_db, monkeypatch):
    threads = [mk_session(f"sweep-{i}") for i in range(3)]
    db = get_db()
    release = asyncio.Event()
    landed = asyncio.Event()
    calls = []

    class Source:
        kind = "t1_watchdog"

        def due(self, now, *, db):
            return [TriggerRequest(self.kind, thread, "scope", thread, "one", reason="watchdog_due")
                    for thread in threads]

        def settle(self, trigger, outcome, reason, *, db):
            return None

    async def deliver(thread, event, summary, details):
        calls.append(thread)
        if len(calls) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            key = _event_idempotency_key(event, summary, details)
            enqueue_event_wake(thread, event, summary, details, message_id=key, db=db)
            enqueue_event_wake(thread, event, summary, details, message_id=key, db=db)
            landed.set()
        return True

    monkeypatch.setattr(execution, "DELIVERY_TIMEOUT_SECONDS", 0.05)
    try:
        result = await runner.sweep("2026-10-27T03:00:00+00:00", db=db, sources=[Source()], deliver=deliver)
        assert len(calls) == 3
        assert result["errored"] == [calls[0]]
        assert set(result["woken"]) == set(calls[1:])
        row = db.execute("SELECT state,idempotency_key FROM continuation_triggers WHERE thread_id=?", (calls[0],)).fetchone()
        assert row["state"] == "failed"
        parked = db.execute("SELECT payload_json FROM dead_letter_events WHERE reason=?", (WAKE_FAILED_REASON,)).fetchall()
        assert any(json.loads(item[0])["message_id"] == row["idempotency_key"] for item in parked)
        release.set()
        await asyncio.wait_for(landed.wait(), 5)
        assert db.execute("SELECT count(*) FROM messages WHERE id=?", (row["idempotency_key"],)).fetchone()[0] == 1
    finally:
        release.set()
        db.close()
