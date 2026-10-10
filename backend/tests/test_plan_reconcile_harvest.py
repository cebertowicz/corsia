"""Child harvest hands the parent executor's slot on at once.

Harvest moves a delegating parent task to review. That task can no longer be
executed, so its attempt is released and the parent plan is reconciled in the
same transaction, selecting the parent's next task.
"""

from __future__ import annotations

from plans import admission, delegation, mutations, service
from tests.test_plan_delegation_rollup import (  # noqa: F401 - shared fixture
    _accept,
    _task,
    db,
)


def _open_attempt(db, node_id: str):
    return db.execute(
        "SELECT * FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
        (node_id,),
    ).fetchone()


def test_harvest_releases_the_parent_attempt_and_selects_the_next_parent_task(
    db, monkeypatch,
):
    child_plan = service.ensure_plan("thread", "child", db=db)
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    delegating = _task(parent_plan.id, "Delegate", spawn_kind="claude-code", db=db)
    next_task = _task(parent_plan.id, "Next parent work", db=db)
    selected = admission.activate(
        delegating.id, expected_revision=delegating.revision, actor_id="parent",
        provenance="test.plan_reconcile_harvest", db=db,
    )
    db.commit()
    parent_attempt = selected["admission"]["selection"]["attempt_id"]
    service.transition_node(next_task.id, "active", db=db)
    # Attached before the child plans anything, the parent keeps its slot: a
    # child with open steps already holds the step, and the attach itself
    # would hand the slot on (matrix row D4).
    mutations.attach_spawned_thread(delegating.id, "child", db=db)
    assert _open_attempt(db, next_task.id) is None
    child_work = _task(child_plan.id, "Child work", db=db)
    _accept(child_work.id, db=db)

    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [delegating.id]

    assert db.in_transaction is False
    assert service.get_node(delegating.id, db=db).state == "review_requested"
    assert db.execute(
        "SELECT released_at FROM plan_node_attempts WHERE id=?", (parent_attempt,),
    ).fetchone()["released_at"] is not None
    successor = _open_attempt(db, next_task.id)
    assert successor is not None, "harvest left the parent's next task unselected"
    assert successor["executor_thread_id"] == "parent"
