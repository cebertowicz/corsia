"""Open time through Plans' real wait read: Turns of the lead and a worker plus a plan wait, one union."""

from __future__ import annotations

from datetime import timedelta

from infra import open_time
from plans import service
from tests.test_plan_node_waiting import T0, _at, _move, _task, _watch, db  # noqa: F401 - db is the fixture


def _turn(db, thread, start, end):
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch) "
        "VALUES (?,?,0,'test/model','completed',?,?,'boot')",
        (f"{thread}-{start}", thread, _at(start), _at(end) if end is not None else None),
    )


def test_a_worker_turn_a_plan_wait_and_the_leads_own_turns_count_once(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id)
    _move(db, node.id, "pending", "active", 0)
    _watch(db, node, 10, resolved=30)
    _turn(db, "owner", 0, 5)
    _turn(db, "child", 2, 12)
    _turn(db, "owner", 30, 35)
    db.commit()

    result = open_time.open_time(db, "owner", since=T0, until=T0 + timedelta(minutes=35), as_of=T0 + timedelta(minutes=60))

    assert result["seconds"] == 35 * 60
    assert result["open_now"] is False
