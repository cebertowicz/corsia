"""A wake about a delegated step stays current only for the worker it names.

PR #2011 review: the currency short-cut for a delegated step's wake must not
outlive the worker it is about (a wake queued before a replace is stale; see
test_plan_replace_worker) nor start a Turn for a step the parent paused or holds.
"""

from __future__ import annotations

from plans import holds, service, wake_currency
from tests.child_parent_notice_helpers import db, delegated

__all__ = ["db"]


def _condition(step, worker: str) -> dict:
    return wake_currency.condition(
        plan_id=step.plan_id, kind="child_returned", subject_id=step.id,
        revision=1, delegated_step_id=step.id, delegated_worker=worker,
    )


def _no_other_work(db, step) -> None:
    """Leave the delegated step as the parent plan's only open work."""
    assert [
        row["id"] for row in db.execute(
            "SELECT id FROM plan_nodes WHERE plan_id=? AND archived_at IS NULL"
            " AND state IN ('pending','active')",
            (step.plan_id,),
        )
    ] == [step.id]


def test_a_wake_from_the_attached_worker_is_current(db):
    step, _plan, _node = delegated(db)
    _no_other_work(db, step)

    assert wake_currency.is_current("parent", _condition(step, "worker"), db=db)


def test_a_wake_about_a_held_step_is_stale(db):
    step, _plan, _node = delegated(db)
    _no_other_work(db, step)
    holds.pause(
        step.id, reason="Thinking it over",
        expected_revision=service.get_node(step.id, db=db).revision,
        actor_id="parent", provenance="test", db=db,
    )

    assert not wake_currency.is_current("parent", _condition(step, "worker"), db=db)
