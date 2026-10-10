"""Whole-plan pause/resume contract for human and agent callers."""

from __future__ import annotations

import sqlite3

import pytest

from automation import plan_liveness, watches
from automation.continuation import triggers
from automation.continuation.models import TriggerRequest
from infra.schema import SCHEMA_SQL
import plans.boundary as boundary
import plans.operator_projection as operator_projection
import plans.service as service
from plans.boundary import PlanActor
from plans.projections import liveness_signal


NOW = "2026-08-14T12:00:00+00:00"


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    for thread_id, user_id in (
        ("owner", "user"),
        ("other", "user"),
        ("foreign", "foreign-user"),
    ):
        db.execute(
            """INSERT INTO threads
               (id,user_id,title,lifecycle_state,created_at,updated_at)
               VALUES (?,?,?,'active','t','t')""",
            (thread_id, user_id, thread_id.title()),
        )
    db.commit()
    return db


def _active_plan(db, owner: str = "owner"):
    plan = service.ensure_plan("thread", owner, db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title="Continue safely",
        acceptance_criteria="The task completes after resumption",
        db=db,
    )
    boundary.mutate(
        plan.id,
        "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        actor=_actor(owner),
        provenance="test.plan_lifecycle",
        mutation_id=f"activate-{owner}",
        db=db,
    )
    return plan, service.get_node(node.id, db=db)


def _actor(thread_id: str = "owner") -> PlanActor:
    return PlanActor(thread_id, "user", None)


def _change(db, plan, operation: str, expected_revision: int, mutation_id: str):
    """Pause or resume as the user, who alone resumes (#1969)."""
    return boundary.mutate(
        plan.id,
        operation,
        {"expected_revision": expected_revision},
        actor=PlanActor(None, "user", None, kind="user"),
        provenance="test.plan_lifecycle",
        mutation_id=mutation_id,
        db=db,
    ).result["plan"]


def _enqueue(db, *, kind: str, thread_id: str, subject_id: str):
    request = TriggerRequest(
        kind=kind,
        thread_id=thread_id,
        scope_id="__workspace__",
        subject_id=subject_id,
        transition=f"due:{subject_id}",
    )
    return triggers.enqueue(request, db=db, now=NOW)[0]


def test_pause_and_resume_are_revisioned_reversible_and_restore_liveness():
    db = _db()
    plan, _node = _active_plan(db)
    wake = plan_liveness.on_turn_complete("owner", db=db)
    assert wake is not None

    current_revision = service._plan_row(db, plan.id)["revision"]
    paused = _change(db, plan, "pause", current_revision, "pause-1")

    assert paused["state"] == "paused"
    assert paused["revision"] == current_revision + 1
    assert liveness_signal("owner", db=db).count == 0
    assert db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE id=?", (wake.id,)
    ).fetchone()["status"] == "cancelled"
    assert plan_liveness.on_turn_complete("owner", db=db) is None

    resumed = _change(db, plan, "resume", paused["revision"], "resume-1")

    assert resumed["state"] == "active"
    assert resumed["revision"] == paused["revision"] + 1
    assert liveness_signal("owner", db=db).count == 1
    assert db.execute(
        """SELECT status FROM plan_liveness_wakes
           WHERE thread_id='owner'"""
    ).fetchone()["status"] == "armed"

    with pytest.raises(service.PlanError, match="illegal plan transition"):
        _change(db, plan, "resume", resumed["revision"], "resume-active")
    with pytest.raises(service.PlanError, match="plan revision conflict"):
        _change(db, plan, "pause", paused["revision"], "pause-stale")


def test_pause_cancels_only_plan_owned_pending_continuations_and_holds_watches():
    db = _db()
    plan, node = _active_plan(db)
    other_plan, other_node = _active_plan(db, "other")
    plan_watch = watches.arm_watch(
        "plan-watch",
        {"type": "deadline", "value": "2000-01-01T00:00:00+00:00"},
        owner_thread="owner",
        plan_id=plan.id,
        plan_node_id=node.id,
        db=db,
    )
    generic_watch = watches.arm_watch(
        "generic-watch",
        {"type": "deadline", "value": "2000-01-02T00:00:00+00:00"},
        owner_thread="owner",
        db=db,
    )
    owned = [
        _enqueue(db, kind="t0_liveness", thread_id="owner", subject_id="wake"),
        _enqueue(db, kind="t1_watchdog", thread_id="owner", subject_id=node.id),
        _enqueue(
            db,
            kind="t2_watch_expiry",
            thread_id="owner",
            subject_id=plan_watch.id,
        ),
    ]
    unrelated = [
        _enqueue(
            db,
            kind="automation_clock",
            thread_id="owner",
            subject_id="schedule",
        ),
        _enqueue(
            db,
            kind="t1_watchdog",
            thread_id="other",
            subject_id=other_node.id,
        ),
    ]
    db.execute(
        """INSERT INTO automation_schedules
           (id,thread_id,scope_id,kind,spec,next_run_at,max_runs,state,
            created_by_actor,created_at,updated_at)
           VALUES ('schedule','owner','__workspace__','clock','once','2099',1,
                   'active','user','t','t')"""
    )
    db.commit()

    paused = _change(
        db, plan, "pause", service._plan_row(db, plan.id)["revision"], "pause-owned"
    )

    for trigger in owned:
        stored = triggers.get(trigger.id, db=db)
        assert (stored.state, stored.reason) == ("cancelled", "plan_paused")
    for trigger in unrelated:
        assert triggers.get(trigger.id, db=db).state == "pending"
    assert db.execute(
        "SELECT state FROM automation_schedules WHERE id='schedule'"
    ).fetchone()["state"] == "active"
    assert [watch.id for watch in watches.due_watches(NOW, db=db)] == [
        generic_watch.id
    ]
    assert watches.get_watch(plan_watch.id, db=db).status == "armed"

    _change(db, plan, "resume", paused["revision"], "resume-owned")
    assert {watch.id for watch in watches.due_watches(NOW, db=db)} == {
        generic_watch.id,
        plan_watch.id,
    }
    assert service._plan_row(db, other_plan.id)["state"] == "active"


def test_pause_is_available_to_the_human_but_not_another_thread():
    db = _db()
    plan, _node = _active_plan(db)
    with pytest.raises(PermissionError, match="owning thread"):
        boundary.mutate(
            plan.id,
            "pause",
            {"expected_revision": service._plan_row(db, plan.id)["revision"]},
            actor=PlanActor("other", "user", None),
            provenance="test",
            mutation_id="other-thread",
            db=db,
        )

    human = boundary.mutate(
        plan.id,
        "pause",
        {"expected_revision": service._plan_row(db, plan.id)["revision"]},
        actor=PlanActor(None, "user", None, kind="user"),
        provenance="test",
        mutation_id="human-pause",
        db=db,
    ).result
    assert human["plan"]["state"] == "paused"


def test_empty_paused_plan_remains_visible_in_the_thread_projection():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    _change(
        db, plan, "pause", service._plan_row(db, plan.id)["revision"], "pause-empty"
    )

    payload = operator_projection.read_thread(
        "owner",
        actor=PlanActor(None, "user", None, kind="user"),
        db=db,
    )

    assert payload["plans"][0]["plan"]["state"] == "paused"
    assert payload["plans"][0]["nodes"] == []
