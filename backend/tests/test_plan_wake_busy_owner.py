"""A busy owner reads one liveness wake per Turn, and the wait it missed isn't counted.

Everything here runs through the real delivery path: the runner hands a due wake
to ``deliver_to_thread``, ``send_thread_input`` queues it behind the running Turn,
and ``claim_safe_boundary`` takes it (or cancels it as stale) at the Turn's
boundary.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from automation import plan_liveness as pl
from automation.continuation import runner
from automation.continuation.sources.liveness import LivenessSource
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


async def _sweep(db, **kwargs) -> dict:
    return await runner.sweep(_NOW, db=db, sources=[LivenessSource()], **kwargs)


@pytest.mark.asyncio
async def test_a_long_turn_reads_one_wake_and_the_cycle_survives_it(isolated_db):
    db, _task = _owner("owner")
    for _ in range(3):
        _make_due(db, "owner")
        await _sweep(db)

    [wake] = _wakes(db, "owner")
    assert (wake["delivery_state"], wake["active_turn_id"]) == ("queued", None)
    assert _cycle(db, "owner") == 0

    claimed = claim_safe_boundary("owner", _TURN, db=db)

    assert claimed is not None
    assert [item.message_id for item in claimed.items].count(wake["id"]) == 1
    [claimed_wake] = _wakes(db, "owner")
    assert claimed_wake["active_turn_id"] == _TURN

    db.execute("UPDATE active_turns SET status='completed' WHERE id=?", (_TURN,))
    db.commit()
    assert _cycle(db, "owner") == 0

    # With the Turn over and nothing queued, an unanswered wake is counted again.
    delivered = []

    async def deliver(*args):
        delivered.append(args)
        return True

    _make_due(db, "owner")
    await _sweep(db, deliver=deliver)
    assert len(delivered) == 1
    assert _cycle(db, "owner") == 1


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
