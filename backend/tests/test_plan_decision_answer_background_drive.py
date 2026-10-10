"""The answer request ends at the durable commit; the owner's drive follows it."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from loguru import logger

from tests.test_plan_decision_answer import (
    _await_detached_drives,
    _client,
    _raised_decision,
    db,  # noqa: F401 - shared fixture
)
from tests.test_plan_decision_delivery import _answer_rows


def _body(decision, mutation_id):
    return {
        "option_id": "A",
        "expected_revision": decision["revision"],
        "mutation_id": mutation_id,
    }


def test_answer_returns_before_a_slow_owner_drive_finishes(db, monkeypatch):
    from infra import thread_execution
    from plans import decisions

    _plan, _source, decision = _raised_decision(db)
    gate = asyncio.Event()
    events: list[tuple[str, str]] = []

    async def slow_prepare(thread_id):
        events.append(("started", thread_id))
        await asyncio.wait_for(gate.wait(), timeout=5)
        events.append(("finished", thread_id))

    monkeypatch.setattr(thread_execution, "drive_thread", slow_prepare)
    # An entered client keeps one loop alive across the request, so the
    # detached drive can be observed while it is still preparing.
    with _client(db, monkeypatch, drain=False) as client:
        response = client.post(
            f"/api/plans/decisions/{decision['id']}/answer",
            json=_body(decision, "answer-bg-slow-prepare"),
        )

        assert response.status_code == 200
        assert decisions.read(decision["id"], db=db)["state"] == "answered"
        assert len(_answer_rows(db, decision["id"])) == 1
        assert ("finished", "owner") not in events

        async def release_and_wait():
            gate.set()
            await _await_detached_drives()

        client.portal.call(release_and_wait)

    # The owner thread is still driven, exactly once, after the response.
    assert events == [("started", "owner"), ("finished", "owner")]


def test_background_drive_failure_is_logged_and_does_not_fail_the_answer(
    db, monkeypatch,
):
    from plans import decisions, notifications

    _plan, _source, decision = _raised_decision(db)
    monkeypatch.setattr(
        notifications, "drive_deferred",
        AsyncMock(side_effect=RuntimeError("drive exploded")),
    )
    client = _client(db, monkeypatch)

    logged: list[str] = []
    sink = logger.add(lambda message: logged.append(str(message)), level="ERROR")
    try:
        response = client.post(
            f"/api/plans/decisions/{decision['id']}/answer",
            json=_body(decision, "answer-bg-drive-raises"),
        )
    finally:
        logger.remove(sink)

    assert response.status_code == 200
    assert decisions.read(decision["id"], db=db)["state"] == "answered"
    assert len(_answer_rows(db, decision["id"])) == 1
    assert any("drive exploded" in line for line in logged)


def test_answer_to_a_busy_owner_still_queues_behind_its_active_input(
    db, monkeypatch,
):
    from infra import thread_execution
    from infra.send_types import SendRejected
    from infra.thread_driver import admit_input

    _plan, _source, decision = _raised_decision(db)
    active = admit_input(
        "owner",
        role="user",
        content="Work already in progress.",
        intent="apply_now",
        message_id="active-input",
        turn_id="active-turn",
        boot_epoch="current-boot",
        admitted_at="2026-09-05T20:00:00+00:00",
        db=db,
    )
    assert active.batch is not None
    drive = AsyncMock(side_effect=SendRejected(409, "thread is busy"))
    monkeypatch.setattr(thread_execution, "drive_thread", drive)

    response = _client(db, monkeypatch).post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json=_body(decision, "answer-bg-busy-owner"),
    )

    assert response.status_code == 200
    [row] = _answer_rows(db, decision["id"])
    assert row["input_order"] > active.accepted.input_order
    drive.assert_awaited_once_with("owner")


def test_a_replayed_answer_redrives_in_the_background(db, monkeypatch):
    from infra import thread_execution

    _plan, _source, decision = _raised_decision(db)
    drive = AsyncMock()
    monkeypatch.setattr(thread_execution, "drive_thread", drive)
    client = _client(db, monkeypatch)
    body = _body(decision, "answer-bg-replay")

    first = client.post(f"/api/plans/decisions/{decision['id']}/answer", json=body)
    second = client.post(f"/api/plans/decisions/{decision['id']}/answer", json=body)

    assert first.status_code == second.status_code == 200
    assert drive.await_count == 2
