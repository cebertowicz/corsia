"""Every wait cause carries a since-time, read from its own canonical row.

Before this: a watch, a raised decision, a delegated-child wait, and a
whole-plan pause each had no timestamp field at all -- only a node-level
hold did (``created_at``, ``plans/holds.py``). A caller could not tell how
long a task had been waiting. Each cause now carries ``created_at`` (the
name holds already expose), read from the exact row that started the wait:
the watch's own ``armed_at``, the decision's own ``raised_at``, the child
attachment's own ``plan_node_events`` row, and the plan's own
``state_transition`` event -- never ``plans.updated_at``/``plan_nodes.
updated_at``, which unrelated later writes keep advancing.
"""

from __future__ import annotations

import uuid

from plans import service
from plans.boundary import PlanActor, mutate
from tests.test_plan_durable_waits import OWNER, _activate, _causes, _db, _raised, _task
import plans.mutations as mutations


def test_watch_cause_carries_its_armed_at():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        watched = _task(db, plan.id, "Await CI")
        watch_id = mutate(
            plan.id, "arm_watch",
            {
                "node_id": watched.id,
                "condition": {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
                "expected_event": "ci.complete",
                "expected_revision": service._node_row(db, watched.id)["revision"],
            },
            actor=OWNER, provenance="test.since_time",
            mutation_id=uuid.uuid4().hex, db=db,
        ).result["watch"]["id"]
        db.commit()

        armed_at = db.execute(
            "SELECT armed_at FROM expected_event_watches WHERE id=?", (watch_id,),
        ).fetchone()["armed_at"]
        cause = _causes(db, watched.id)[0]
        assert cause["created_at"] is not None
        assert cause["created_at"] == armed_at
    finally:
        db.close()


def test_raised_decision_cause_carries_its_raised_at():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        deciding = _task(db, plan.id, "Name the feature")
        _activate(db, deciding.id)
        decision = _raised(db, plan, deciding)
        db.commit()

        cause = _causes(db, deciding.id)[0]
        assert cause["reason_key"] == "decision_raised"
        assert cause["created_at"] is not None
        assert cause["created_at"] == decision["raised_at"]
    finally:
        db.close()


def test_delegated_child_cause_carries_its_attachment_time():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        step = _task(db, plan.id, "Delegated research", spawn_kind="claude-code")
        _activate(db, step.id)
        child_plan = service.ensure_plan("thread", "child", db=db)
        child_task = _task(db, child_plan.id, "Gather sources")
        mutations.attach_spawned_thread(step.id, "child", db=db)
        _activate(db, child_task.id, actor_id="child")
        db.commit()

        attached_at = db.execute(
            "SELECT created_at FROM plan_node_events "
            "WHERE node_id=? AND event='spawned_thread_attached'",
            (step.id,),
        ).fetchone()["created_at"]

        cause = _causes(db, step.id)[0]
        assert cause["reason_key"] == "delegated_child"
        assert cause["created_at"] is not None
        assert cause["created_at"] == attached_at
    finally:
        db.close()


def test_plan_pause_cause_carries_its_pause_time_not_a_later_unrelated_write():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        node = _task(db, plan.id, "Continue safely")
        _activate(db, node.id)
        db.commit()

        mutate(
            plan.id, "pause", {"expected_revision": service._plan_row(db, plan.id)["revision"]},
            actor=PlanActor(None, "user", None, kind="user"), provenance="test.since_time",
            mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()

        paused_at = db.execute(
            "SELECT created_at FROM plan_events "
            "WHERE plan_id=? AND event='state_transition' AND new_state='paused'",
            (plan.id,),
        ).fetchone()["created_at"]
        cause = _causes(db, node.id)[0]
        assert cause["reason_key"] == "plan_paused"
        assert cause["created_at"] == paused_at

        # A later, unrelated write to the plan (a decision drafted on a
        # second task) must never advance the shown pause time: only the
        # plan's own state_transition event may.
        other = _task(db, plan.id, "Unrelated later work")
        _activate(db, other.id, actor_id="owner")
        from plans import decisions

        decisions.draft(
            plan.id, source_node_id=other.id, mutation_id=uuid.uuid4().hex,
            actor=OWNER, provenance="test.since_time", db=db,
            title="Unrelated question", context="c", tradeoff="t",
            recommendation="A", rationale="r",
            options=[
                {"id": "A", "label": "A", "detail": "d"},
                {"id": "B", "label": "B", "detail": "d"},
            ],
            other_enabled=True,
        )
        db.commit()

        assert _causes(db, node.id)[0]["created_at"] == paused_at
    finally:
        db.close()
