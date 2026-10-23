"""A pending switch has a reachable exit from passive-only and failed sources.

A passive digest waits for activity and never starts a Turn, so a switch that
waited for it to drain waited forever (#1880). It is context, not source work:
the switch proceeds and the digest rides to the destination at its FIFO place.
A failed switch returns inputs to the source, and the failure itself is their
wake (R16); nothing else would ever start them.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from infra.database import get_db, write_transaction
from infra.input_admission import accept_thread_input
from infra.model_switches import activate_ready_switch, read_switch, request_switch
from infra.thread_inputs import start_idle_turn

SOURCE = "local/source"
DESTINATION = "native/destination"
THREAD = "passive-thread"


def _passive_digest(db, message_id: str = "digest-1"):
    return accept_thread_input(
        THREAD, role="system", content="[PASSIVE CONTEXT DIGEST]\n1 update",
        intent="queue_after_turn", message_id=message_id,
        metadata={
            "type": "context_event",
            "event_type": "context.digest",
            "event_disposition": "passive",
        },
        db=db,
    )


@pytest.fixture
def passive_source(isolated_db):
    """A source whose only queued input is a passive digest."""
    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?,'user-1',?,'t0','t0')", (THREAD, SOURCE),
    )
    db.commit()
    _passive_digest(db)
    try:
        yield db
    finally:
        db.close()


def _thread(db):
    return tuple(db.execute(
        "SELECT model,execution_epoch,switch_status FROM threads WHERE id=?", (THREAD,),
    ).fetchone())


def test_passive_only_source_switch_activates_and_carries_the_digest(passive_source):
    db = passive_source
    request = request_switch(
        THREAD, DESTINATION, operation_id="switch-1", requested_at="t1", db=db,
    )
    assert not request.waiting_for_source  # A digest is not work to drain.

    with write_transaction(db, scope="test_passive_switch_commit") as connection:
        connection.execute(
            "UPDATE threads SET switch_status='ready' "
            "WHERE switch_operation_id='switch-1'",
        )
        ready = activate_ready_switch(connection, THREAD, committed_at="t2")

    assert ready is not None and ready.committed is True
    assert _thread(db) == (DESTINATION, 1, None)
    accept_thread_input(
        THREAD, role="user", content="hello", intent="apply_now",
        message_id="input-1", db=db,
    )
    batch = start_idle_turn(
        THREAD, turn_id="turn-1", boot_epoch="boot", started_at="t3", db=db,
    )
    assert batch is not None
    assert batch.target_model == DESTINATION
    assert [item.message_id for item in batch.items] == ["digest-1", "input-1"]


def test_real_source_work_still_holds_the_switch(passive_source):
    """Only passive context rides along; a Turn-starting input still drains first."""
    db = passive_source
    accept_thread_input(
        THREAD, role="user", content="before", intent="apply_now",
        message_id="before", db=db,
    )
    request = request_switch(
        THREAD, DESTINATION, operation_id="switch-1", requested_at="t1", db=db,
    )
    assert request.waiting_for_source
    with write_transaction(db, scope="test_held_switch_ready") as connection:
        connection.execute(
            "UPDATE threads SET switch_status='ready' "
            "WHERE switch_operation_id='switch-1'",
        )
        ready = activate_ready_switch(connection, THREAD, committed_at="t2")
    assert ready is None
    assert _thread(db) == (SOURCE, 0, "ready")


@pytest.mark.asyncio
async def test_startup_wake_activates_a_switch_stuck_behind_a_digest(
    passive_source, monkeypatch,
):
    """The boot wake is the recovery for a thread already stuck this way."""
    from automation import restart_startup
    from loguru import logger
    from thread import model_pending

    request_switch(
        THREAD, DESTINATION, operation_id="switch-1", requested_at="t1",
        db=passive_source,
    )

    async def commit(request):
        with write_transaction(None, scope="test_startup_switch_commit") as connection:
            connection.execute(
                "UPDATE threads SET switch_status='ready' "
                "WHERE switch_operation_id=?",
                (request.operation_id,),
            )
            return activate_ready_switch(
                connection, request.thread_id, committed_at="t2",
            )

    monkeypatch.setattr(model_pending, "_commit_fitting_switch", commit)
    monkeypatch.setattr(restart_startup, "_replay_held_webhooks", AsyncMock())
    recovered, wakes, _rolled_back = await restart_startup.reconcile_startup_continuity(logger)
    assert THREAD in wakes

    await restart_startup.drive_startup_continuity(recovered, wakes, logger)

    assert read_switch(THREAD) is None
    assert _thread(passive_source) == (DESTINATION, 1, None)
    assert passive_source.execute(
        "SELECT target_model,target_execution_epoch FROM messages WHERE id='digest-1'",
    ).fetchone()[:] == (DESTINATION, 1)


@pytest.mark.asyncio
async def test_failed_switch_starts_the_source_on_its_returned_input(
    isolated_db, monkeypatch,
):
    """No new input arrives; the failure alone must start the returned one."""
    from infra import thread_execution
    from thread import model_pending

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?,'user-1',?,'t0','t0')", (THREAD, SOURCE),
    )
    db.commit()
    request = request_switch(
        THREAD, DESTINATION, operation_id="switch-1", requested_at="t1", db=db,
    )
    accepted = accept_thread_input(
        THREAD, role="user", content="waiting", intent="apply_now",
        message_id="input-1", db=db,
    )
    db.close()
    assert accepted.target_model == DESTINATION
    started = []

    async def drive(batch):
        started.append(batch)
        return SimpleNamespace()

    monkeypatch.setattr(thread_execution, "drive_claimed_batch", drive)

    await model_pending._fail_switch(request, "cold checkpoint does not fit")

    assert len(started) == 1
    assert started[0].target_model == SOURCE
    assert [item.message_id for item in started[0].items] == ["input-1"]
