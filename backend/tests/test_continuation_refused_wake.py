"""A continuation wake refused before its owner accepted it is reported, never silently spent (#2446).

A deadline-watch wake refused at delivery settles ``failed`` and expires the
watch. A wake the owner's FIFO accepted is reported when its row settles
failed (``infra.failed_wake_surface``); one refused before acceptance leaves no
row, so the continuation settlement reports it through the same surface.
"""

from __future__ import annotations

import json

import pytest

from automation.continuation import runner
from automation.continuation.sources.watch_expiry import WatchExpirySource
from events import delivery
from infra.failed_wake_surface import REPORT_EVENT, WAKE_FAILED_REASON
from infra.queued_input_settlement import fail_queued
from tests.test_continuation_watch_expiry import _arm, _db, _plan

NOW = "2026-09-26T16:00:00+00:00"


def _armed_watch(parent: str | None = None):
    db = _db()
    db.execute("UPDATE threads SET model='test-model'")  # a FIFO accepts input only for a bound model
    if parent is not None:
        db.execute("UPDATE threads SET parent_thread_id=? WHERE id='owner'", (parent,))
    plan, node, _other = _plan(db)
    watch = _arm(db, plan, node)["watch"]
    db.commit()
    return db, watch


async def _sweep(db, deliver):
    return await runner.sweep(
        NOW, db=db, sources=[WatchExpirySource()],
        is_idle=lambda _thread: True, deliver=deliver,
    )


def _settled(db, watch) -> tuple[str, str, str]:
    trigger = db.execute("SELECT state,idempotency_key FROM continuation_triggers").fetchone()
    status = db.execute(
        "SELECT status FROM expected_event_watches WHERE id=?", (watch["id"],),
    ).fetchone()["status"]
    return trigger["state"], status, trigger["idempotency_key"]


def _parked(db) -> list[dict]:
    return [
        json.loads(row["payload_json"]) for row in db.execute(
            "SELECT payload_json FROM dead_letter_events WHERE reason=?", (WAKE_FAILED_REASON,),
        ).fetchall()
    ]


def _reports(db, thread: str) -> list:
    return db.execute(
        "SELECT content FROM messages WHERE thread_id=? AND metadata LIKE ?",
        (thread, f"%{REPORT_EVENT}%"),
    ).fetchall()


async def _refused(*_args) -> bool:
    return False


@pytest.mark.asyncio
async def test_a_refused_watch_wake_without_an_owner_side_is_parked():
    db, watch = _armed_watch()

    result = await _sweep(db, _refused)

    state, status, key = _settled(db, watch)
    assert result["errored"] == ["owner"] and (state, status) == ("failed", "expired")
    [parked] = _parked(db)
    assert (parked["thread_id"], parked["message_id"]) == ("owner", key)
    assert parked["event"] == "plan.watch_expired"


@pytest.mark.asyncio
async def test_a_refused_watch_wake_is_reported_to_its_parent():
    db, watch = _armed_watch(parent="other")

    await _sweep(db, _refused)

    [report] = _reports(db, "other")
    assert "owner" in report["content"] and "refused before the thread accepted it" in report["content"]
    assert _settled(db, watch)[:2] == ("failed", "expired") and _parked(db) == []


@pytest.mark.asyncio
async def test_a_wake_the_fifo_accepted_is_left_to_its_own_settlement():
    db, _watch = _armed_watch(parent="other")

    async def accepted_then_refused(thread_id, event, summary, details):
        key = delivery._event_idempotency_key(event, summary, details)
        delivery.enqueue_event_wake(thread_id, event, summary, details, message_id=key, db=db)
        db.commit()
        return False

    await _sweep(db, accepted_then_refused)

    assert _reports(db, "other") == [] and _parked(db) == []


@pytest.mark.asyncio
async def test_a_watch_record_is_corrected_when_its_accepted_wake_fails(monkeypatch):
    from automation.continuation import execution

    db, watch = _armed_watch(parent="other")
    message_id = None
    events: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        execution, "emit_product_event",
        lambda category, action, *, properties: events.append(
            (category, action, properties.get("corrects")),
        ),
    )

    async def accepted(thread_id, event, summary, details):
        nonlocal message_id
        message_id = delivery._event_idempotency_key(event, summary, details)
        delivery.enqueue_event_wake(thread_id, event, summary, details, message_id=message_id, db=db)
        db.commit()
        return True

    await _sweep(db, accepted)
    message = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()

    fail_queued(db, "owner", [message], error="model unavailable at admission")

    assert _settled(db, watch)[:2] == ("failed", "expired")
    assert events == [("continuation", "delivered", None), ("continuation", "failed", "delivered")]
