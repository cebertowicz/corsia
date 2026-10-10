"""A worker's plan shows under the one node that holds its newest attachment.

A reused worker (a follow-up, or a revision after a rejection) is named by
more than one node in the delegating plan. Its plan nests under the node it is
working for, never under the finished original beside it. A worker that was
not reused keeps showing its plan under its settled step, so the drill-in
survives.
"""

from __future__ import annotations

from plans import mutations, rollup, service
from tests.plan_review_helpers import submit_review_evidence
from tests.test_plan_rejection_revision import _reject, _revision_of
from tests.test_plan_verdict_settles_return import _db, _returned, _verdict


def _child_plans(db, plan_id: str) -> dict[str, object]:
    projected = rollup.rollup_thread(plan_id, db=db)
    return {node["id"]: node["child_plan"] for node in projected.nodes}


def _worker_takes_new_step(db, plan_id: str) -> None:
    step = service.add_node(
        plan_id, kind="task", title="Second round", acceptance_criteria="Done", db=db,
    )
    service.transition_node(step.id, "active", actor_id="worker", db=db)
    db.commit()


def test_a_follow_up_that_reuses_the_worker_shows_its_plan_not_the_original(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, _leaves = _returned(db, "Write the tests")
        submit_review_evidence(delegated.id, db=db, actor_id="parent")
        result = _verdict(
            db, parent_plan.id, "accept_with_follow_up", delegated.id,
            title="Polish", acceptance_criteria="Polished",
        )
        follow_up = result["follow_up"]["id"]
        service.transition_node(follow_up, "active", db=db)
        mutations.attach_spawned_thread(follow_up, "worker", db=db)
        _worker_takes_new_step(db, plan.id)

        shown = _child_plans(db, parent_plan.id)

        assert shown[delegated.id] is None
        assert shown[follow_up] is not None
        assert shown[follow_up].plan["id"] == plan.id
    finally:
        db.close()


def test_a_revision_that_takes_the_worker_shows_its_plan_not_the_rejected_step(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, _leaves = _returned(db, "Write the tests")
        _reject(db, parent_plan.id, delegated.id)
        revision = _revision_of(db, delegated.id)
        assert revision["spawned_thread_id"] == "worker"
        _worker_takes_new_step(db, plan.id)

        shown = _child_plans(db, parent_plan.id)

        # The rejected step is history of the revision, not a row of its own.
        assert delegated.id not in shown
        assert shown[revision["id"]] is not None
    finally:
        db.close()


def test_a_settled_step_whose_worker_was_not_reused_still_shows_its_plan(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, _leaves = _returned(db, "Write the tests")
        submit_review_evidence(delegated.id, db=db, actor_id="parent")
        _verdict(db, parent_plan.id, "accept", delegated.id)
        db.commit()

        shown = _child_plans(db, parent_plan.id)

        assert service.get_node(delegated.id, db=db).state == "accepted"
        assert shown[delegated.id] is not None
        assert shown[delegated.id].plan["id"] == plan.id
    finally:
        db.close()
