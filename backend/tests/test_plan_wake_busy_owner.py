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
from infra.input_writes import begin_batch_write
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


def _claim_and_start_write(db, thread_id: str, *, now: datetime):
    """Use the real FIFO claim and write-start timestamp for one live Turn."""
    claimed = claim_safe_boundary(thread_id, _TURN, db=db)
    assert claimed is not None
    begin_batch_write(
        claimed,
        attempt_id=f"{thread_id}:{now.isoformat()}",
        started_at=now.isoformat(),
        db=db,
    )
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

    claimed = _claim_and_start_write(db, "owner", now=clock[0])
    assert [item.message_id for item in claimed.items].count(wake["id"]) == 1
    [claimed_wake] = _wakes(db, "owner")
    assert claimed_wake["active_turn_id"] == _TURN

    clock[0] += timedelta(seconds=pl.PLAN_WAKE_IDLE_SECONDS)
    assert (await _sweep(db, clock[0].isoformat()))["woken"] == []
    assert len(_wakes(db, "owner")) == 1

    clock[0] += timedelta(
        seconds=pl.PLAN_WAKE_IN_TURN_SECONDS - pl.PLAN_WAKE_IDLE_SECONDS,
    )
    assert (await _sweep(db, clock[0].isoformat()))["woken"] == ["owner"]
    assert len(_wakes(db, "owner")) == 2
    assert _cycle(db, "owner") == 0

    _claim_and_start_write(db, "owner", now=clock[0])
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
    armed = db.execute(
        "SELECT deadline FROM plan_liveness_wakes WHERE thread_id=?", ("owner",),
    ).fetchone()
    assert _cycle(db, "owner") == 1
    assert datetime.fromisoformat(armed["deadline"]) - clock[0] == timedelta(
        seconds=pl.PLAN_WAKE_IDLE_SECONDS * pl.PLAN_WAKE_BACKOFF,
    )


@pytest.mark.asyncio
async def test_a_late_claim_does_not_rearm_inside_the_flat_interval(isolated_db, monkeypatch):
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(pl, "_now", lambda: clock[0])
    db, _task = _owner("owner")
    _make_due(db, "owner")
    await _sweep(db, clock[0].isoformat())

    clock[0] += timedelta(seconds=pl.PLAN_WAKE_IDLE_SECONDS + 1)
    _claim_and_start_write(db, "owner", now=clock[0])

    assert (await _sweep(db, clock[0].isoformat()))["woken"] == []
    assert len(_wakes(db, "owner")) == 1
    assert _cycle(db, "owner") == 0


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
