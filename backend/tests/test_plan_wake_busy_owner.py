"""A busy owner reads one liveness wake per Turn, and the wait it missed isn't counted.

The in-Turn cases use the real runner, event delivery, FIFO queue and boundary
claim path. The post-Turn idle-backoff assertion stubs delivery: without a live
Turn, real event delivery starts a provider dispatch outside this unit boundary.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from automation import plan_liveness as pl
from automation.continuation import runner
from automation.continuation.sources.liveness import LivenessSource
from infra.input_writes import (
    begin_batch_write,
    release_batch_after_proved_no_write,
    settle_batch_write,
)
from infra.thread_inputs import claim_safe_boundary
from plans import admission, service
from tests.event_routing_helpers import mk_session

_PAST = "2000-01-01T00:00:00+00:00"
_NOW = "2099-01-01T00:00:00+00:00"
_TURN = "turn-busy"


def _owner(thread_id: str):
    """An owner with one actionable task, an armed due wake and a running Turn."""
    from infra.database import get_db

    mk_session(thread_id)
    db = get_db()
    plan = service.ensure_plan("thread", thread_id, db=db)
    task = service.add_node(
        plan.id, kind="task", title="Task", acceptance_criteria="Done", db=db,
    )
    service.transition_node(task.id, "active", db=db)
    admission.reconcile_plan(plan.id, actor_id=thread_id, provenance="test", db=db)
    pl.on_turn_complete(thread_id, db=db)
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "SELECT ?,id,execution_epoch,model,'running','2000-01-01T00:00:00','boot-1' "
        "FROM threads WHERE id=?",
        (_TURN, thread_id),
    )
    db.commit()
    return db, task


def _make_due(db, thread_id: str) -> None:
    db.execute(
        "UPDATE plan_liveness_wakes SET deadline=? WHERE thread_id=?", (_PAST, thread_id),
    )
    db.commit()


def _cycle(db, thread_id: str) -> int:
    return db.execute(
        "SELECT cycle FROM plan_liveness_wakes WHERE thread_id=?", (thread_id,),
    ).fetchone()["cycle"]


def _deadline(db, thread_id: str) -> datetime:
    return datetime.fromisoformat(db.execute(
        "SELECT deadline FROM plan_liveness_wakes WHERE thread_id=?", (thread_id,),
    ).fetchone()["deadline"])


def _wakes(db, thread_id: str) -> list[sqlite3.Row]:
    """Every liveness wake input the thread holds, oldest first."""
    return [
        row
        for row in db.execute(
            "SELECT id,delivery_state,active_turn_id,metadata FROM messages "
            "WHERE thread_id=? ORDER BY input_order",
            (thread_id,),
        )
        if json.loads(row["metadata"] or "{}").get("event_type") == "plan.liveness_wake"
    ]


async def _sweep(db, now: str = _NOW, **kwargs) -> dict:
    return await runner.sweep(now, db=db, sources=[LivenessSource()], **kwargs)


def _claim_and_settle_write(
    db, thread_id: str, *, now: datetime, outcome: str = "accepted",
):
    """Run the real FIFO claim and provider-write settlement for one live Turn."""
    claimed = claim_safe_boundary(thread_id, _TURN, db=db)
    assert claimed is not None
    attempt_id = f"{thread_id}:{now.isoformat()}"
    begin_batch_write(
        claimed,
        attempt_id=attempt_id,
        started_at=now.isoformat(),
        db=db,
    )
    if outcome == "proved_no_write":
        release_batch_after_proved_no_write(
            claimed, attempt_id=attempt_id, error=None, db=db,
        )
    else:
        settle_batch_write(attempt_id, outcome=outcome, db=db)
    return claimed


@pytest.mark.asyncio
async def test_a_long_turn_uses_a_flat_interval_from_the_claim(isolated_db, monkeypatch):
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(pl, "_now", lambda: clock[0])
    db, _task = _owner("owner")
    _make_due(db, "owner")

    assert (await _sweep(db, clock[0].isoformat()))["woken"] == ["owner"]
    [wake] = _wakes(db, "owner")
    assert (wake["delivery_state"], wake["active_turn_id"]) == ("queued", None)
    assert _cycle(db, "owner") == 0
    assert _deadline(db, "owner") - clock[0] == timedelta(seconds=pl.PLAN_WAKE_IN_TURN_SECONDS)

    claimed = _claim_and_settle_write(db, "owner", now=clock[0])
    assert [item.message_id for item in claimed.items].count(wake["id"]) == 1
    [claimed_wake] = _wakes(db, "owner")
    assert (claimed_wake["active_turn_id"], claimed_wake["delivery_state"]) == (
        _TURN, "delivered",
    )

    clock[0] += timedelta(seconds=pl.PLAN_WAKE_IDLE_SECONDS)
    assert (await _sweep(db, clock[0].isoformat()))["woken"] == []
    assert len(_wakes(db, "owner")) == 1

    clock[0] += timedelta(
        seconds=pl.PLAN_WAKE_IN_TURN_SECONDS - pl.PLAN_WAKE_IDLE_SECONDS,
    )
    assert (await _sweep(db, clock[0].isoformat()))["woken"] == ["owner"]
    assert len(_wakes(db, "owner")) == 2
    assert _cycle(db, "owner") == 0

    _claim_and_settle_write(db, "owner", now=clock[0])
    db.execute("UPDATE active_turns SET status='completed' WHERE id=?", (_TURN,))
    db.commit()

    # With the Turn over and nothing queued, idle no-progress backoff doubles again.
    delivered = []

    async def deliver(*args):
        delivered.append(args)
        return True

    clock[0] += timedelta(seconds=1)
    _make_due(db, "owner")
    assert (await _sweep(db, clock[0].isoformat(), deliver=deliver))["woken"] == ["owner"]
    assert len(delivered) == 1
    assert _cycle(db, "owner") == 1
    assert _deadline(db, "owner") - clock[0] == timedelta(
        seconds=pl.PLAN_WAKE_IDLE_SECONDS * pl.PLAN_WAKE_BACKOFF,
    )


@pytest.mark.asyncio
async def test_a_late_claim_does_not_rearm_inside_the_flat_interval(isolated_db, monkeypatch):
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(pl, "_now", lambda: clock[0])
    db, _task = _owner("owner")
    _make_due(db, "owner")
    start = clock[0]
    await _sweep(db, start.isoformat())

    # The boundary claims the reminder halfway to its next deadline.
    claimed_at = start + timedelta(seconds=pl.PLAN_WAKE_IN_TURN_SECONDS / 2)
    _claim_and_settle_write(db, "owner", now=claimed_at)

    clock[0] = start + timedelta(seconds=pl.PLAN_WAKE_IN_TURN_SECONDS)
    assert (await _sweep(db, clock[0].isoformat()))["woken"] == []
    assert len(_wakes(db, "owner")) == 1
    assert _cycle(db, "owner") == 0

    clock[0] = claimed_at + timedelta(seconds=pl.PLAN_WAKE_IN_TURN_SECONDS + 1)
    assert (await _sweep(db, clock[0].isoformat()))["woken"] == ["owner"]
    assert len(_wakes(db, "owner")) == 2


@pytest.mark.asyncio
async def test_a_turn_after_idle_backoff_still_gets_the_flat_interval(isolated_db, monkeypatch):
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(pl, "_now", lambda: clock[0])
    db, _task = _owner("owner")
    db.execute("UPDATE plan_liveness_wakes SET cycle=3 WHERE thread_id=?", ("owner",))
    _make_due(db, "owner")

    assert (await _sweep(db, clock[0].isoformat()))["woken"] == ["owner"]

    assert _cycle(db, "owner") == 3
    assert _deadline(db, "owner") - clock[0] == timedelta(seconds=pl.PLAN_WAKE_IN_TURN_SECONDS)


@pytest.mark.asyncio
async def test_an_unwritten_wake_holds_the_next_one_until_it_is_read(
    isolated_db, monkeypatch,
):
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(pl, "_now", lambda: clock[0])
    db, _task = _owner("owner")
    _make_due(db, "owner")
    await _sweep(db, clock[0].isoformat())

    _claim_and_settle_write(db, "owner", now=clock[0], outcome="proved_no_write")
    [released] = _wakes(db, "owner")
    assert (released["active_turn_id"], released["delivery_state"]) == (None, "queued")

    # The released wake was never read, so it is still the pending reminder.
    clock[0] += timedelta(seconds=pl.PLAN_WAKE_IN_TURN_SECONDS)
    assert (await _sweep(db, clock[0].isoformat()))["woken"] == []

    # Claimed into the running Turn but not yet written: still no second wake.
    claimed = claim_safe_boundary("owner", _TURN, db=db)
    assert claimed is not None
    clock[0] += timedelta(seconds=pl.PLAN_WAKE_IN_TURN_SECONDS)
    assert (await _sweep(db, clock[0].isoformat()))["woken"] == []
    assert len(_wakes(db, "owner")) == 1

    # Once written, the next one comes a flat interval after that read.
    attempt_id = f"owner:{clock[0].isoformat()}"
    begin_batch_write(claimed, attempt_id=attempt_id, started_at=clock[0].isoformat(), db=db)
    settle_batch_write(attempt_id, outcome="accepted", db=db)
    read_at = clock[0]
    clock[0] = read_at + timedelta(seconds=pl.PLAN_WAKE_IN_TURN_SECONDS - 1)
    assert (await _sweep(db, clock[0].isoformat()))["woken"] == []
    clock[0] = read_at + timedelta(seconds=pl.PLAN_WAKE_IN_TURN_SECONDS + 1)
    assert (await _sweep(db, clock[0].isoformat()))["woken"] == ["owner"]
    assert len(_wakes(db, "owner")) == 2


@pytest.mark.asyncio
async def test_a_claim_left_by_a_finished_turn_does_not_hold_wakes(isolated_db, monkeypatch):
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(pl, "_now", lambda: clock[0])
    db, _task = _owner("owner")
    _make_due(db, "owner")
    await _sweep(db, clock[0].isoformat())
    assert claim_safe_boundary("owner", _TURN, db=db) is not None
    db.execute("UPDATE active_turns SET status='completed' WHERE id=?", (_TURN,))
    db.commit()

    delivered = []

    async def deliver(*args):
        delivered.append(args)
        return True

    clock[0] += timedelta(seconds=pl.PLAN_WAKE_IN_TURN_SECONDS + 1)
    _make_due(db, "owner")
    assert (await _sweep(db, clock[0].isoformat(), deliver=deliver))["woken"] == ["owner"]
    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_a_wake_whose_work_is_gone_is_cancelled_at_the_boundary(isolated_db):
    db, task = _owner("owner")
    _make_due(db, "owner")
    await _sweep(db)
    [queued] = _wakes(db, "owner")

    service.transition_node(task.id, "closed", db=db)
    db.commit()

    assert claim_safe_boundary("owner", _TURN, db=db) is None
    [cancelled] = _wakes(db, "owner")
    assert (cancelled["id"], cancelled["delivery_state"]) == (queued["id"], "cancelled")
