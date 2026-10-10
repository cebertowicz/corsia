"""Matrix section A: what a parent hears about its worker's step lifecycle.

One idea runs through every case. A delegated worker's ordinary progress is a
**digest** entry the parent reads at its next Turn boundary; the parent is
**woken** only when it has something to do -- a result to judge, a failure to
resolve, or a worker that stopped.

Section B (conditions inside the worker) and section E (decisions) are in
``test_child_parent_notice_conditions.py``; both share one lineage fixture.
"""

from __future__ import annotations

import json

from automation.turn_end_effects import PendingTurnEndEffects
from automation import plan_liveness_terminal
from plans import closure, mutations, service
from tests.child_parent_notice_helpers import (
    begin_worker_turn,
    db,
    delegated,
    end_worker_turn,
    notice_details,
    notice_kinds,
    notices,
    other_open_step,
    wakes,
)
from tests.plan_review_helpers import submit_review_evidence

__all__ = ["db"]


# --- A. Child plan node lifecycle --------------------------------------------


def test_a3_child_self_review_digests_instead_of_waking_the_parent(db):
    """A3: the child reviews its own step, so the parent has nothing to do."""
    _step, plan, node = delegated(db)
    other_open_step(db, plan)
    service.transition_node(node.id, "active", actor_id="worker", db=db)

    submit_review_evidence(node.id, db=db, actor_id="worker")

    assert notice_kinds(db) == ["plan.child_node_review_requested"]
    assert wakes(db) == []


def test_a6_failed_child_step_wakes_the_parent_once_at_turn_end(db):
    """A6: a failure left standing when the worker stops is the parent's to decide.

    Not at the failure itself: the worker may still retry or re-scope it in
    the same Turn (#2216).
    """
    _step, plan, node = delegated(db)
    other_open_step(db, plan)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    begin_worker_turn(db)

    mutations.fail_node(node.id, actor_id="worker", db=db)
    assert wakes(db) == []
    end_worker_turn(db)
    end_worker_turn(db)

    appended = wakes(db)
    assert [row["id"] for row in appended] == ["plan-wake:child_step_failed:turn-1:0"]
    content = str(appended[0]["content"])
    assert json.loads(content.splitlines()[-1])["failed_node_ids"] == [node.id]
    # Rule 7: the wake names the effect on the parent and the one action.
    assert "no return is coming yet" in content
    assert "retry" in content.lower()


def test_a6_names_close_for_a_step_that_was_only_replaced(db):
    """#2232: a replaced step is not a failure, and the wake says what to do instead."""
    _step, plan, node = delegated(db)
    other_open_step(db, plan)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    begin_worker_turn(db)
    mutations.fail_node(node.id, actor_id="worker", db=db)

    end_worker_turn(db)

    content = str(wakes(db)[0]["content"]).splitlines()[0]
    assert "close_node, not fail" in content and "archive" in content


def test_a6_a_rebriefed_worker_that_closes_replaced_steps_is_nobodys_failure(db):
    """#2232 re-brief: the replaced steps close, a new one starts, the parent hears no failure."""
    _step, plan, first = delegated(db)
    second = other_open_step(db, plan, title="Verify and open the PR")
    service.transition_node(first.id, "active", actor_id="worker", db=db)
    begin_worker_turn(db)

    for replaced in (first, second):
        closure.close_node(
            replaced.id, reason="Replaced by the new brief", actor_id="worker", db=db,
        )
    other_open_step(db, plan, title="The new brief's step")
    end_worker_turn(db)

    # It stopped with the new step open, so F2 (#2539) tells the parent; no
    # wake says anything failed.
    assert [str(row["id"]).split(":")[1] for row in wakes(db)] == ["child_stopped_open"]
    assert "failed" not in {
        row["state"] for row in db.execute(
            "SELECT state FROM plan_nodes WHERE plan_id=?", (plan.id,),
        ).fetchall()
    }


def test_a6_failure_from_an_earlier_turn_is_not_raised_again(db):
    """Only this Turn's failures: an older one was already the parent's to hear."""
    _step, plan, node = delegated(db)
    other_open_step(db, plan)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    mutations.fail_node(node.id, actor_id="worker", db=db)
    begin_worker_turn(db, "turn-later", started_at="2999-01-01T00:00:00")

    end_worker_turn(db)

    assert not any("child_step_failed" in str(row["id"]) for row in wakes(db))


def test_a6_last_step_failure_is_left_to_the_harvest(db):
    """A7 stays one wake: the worker's final failure returns through harvest."""
    _step, _plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    begin_worker_turn(db)

    mutations.fail_node(node.id, actor_id="worker", db=db)
    end_worker_turn(db)

    assert not any("child_step_failed" in str(row["id"]) for row in wakes(db))


def test_a6_settled_delegating_step_hears_no_late_wake(db):
    """#1991: once the parent settled its step, the worker's news decides nothing."""
    from plans.delegation_notice import _wake_parent

    step, plan, _node = delegated(db)

    def stalled(subject_id: str) -> None:
        _wake_parent(
            db, step.plan_id, "worker", kind="child_stopped_open",
            subject_id=subject_id, revision=0,
        )

    stalled("while-open")
    assert len(wakes(db)) == 1
    mutations.fail_node(step.id, actor_id="parent", db=db)

    stalled("after-settle")

    assert len(wakes(db)) == 1


def test_a8_closed_child_step_digests_with_its_reason(db):
    """A8: work the worker will not do is progress, and the reason travels."""
    _step, plan, node = delegated(db)
    other_open_step(db, plan)

    closure.close_node(
        node.id, reason="Superseded by the shared helper", actor_id="worker", db=db,
    )

    assert wakes(db) == []
    assert notice_kinds(db) == ["plan.child_node_closed"]
    row = notices(db)[0]
    assert "Superseded by the shared helper" in str(row["summary"])
    assert notice_details(row)["reason"] == "Superseded by the shared helper"


def test_a11_child_that_plans_nothing_wakes_the_parent_once(db):
    """A11: a worker whose Turn ends with an empty plan returned nothing."""
    from automation import plan_liveness

    _step, _plan, _node = delegated(db, with_worker_node=False)

    plan_liveness_terminal.on_thread_turn_complete(
        "worker",
        db=db,
        effects=PendingTurnEndEffects(),
    )
    plan_liveness_terminal.on_thread_turn_complete(
        "worker",
        db=db,
        effects=PendingTurnEndEffects(),
    )

    appended = wakes(db)
    assert len(appended) == 1
    assert "returned nothing" in str(appended[0]["content"]).lower()
    assert "re-brief" in str(appended[0]["content"]).lower()


def test_a11_stays_quiet_while_the_child_has_planned_work(db):
    """A11 is about an empty plan, not about a Turn that simply ended."""
    from automation import plan_liveness

    delegated(db)

    plan_liveness_terminal.on_thread_turn_complete(
        "worker",
        db=db,
        effects=PendingTurnEndEffects(),
    )

    # Open work that simply stopped is F2's wake, never "returned nothing".
    assert [str(row["id"]).split(":")[1] for row in wakes(db)] == ["child_stopped_open"]


def test_a11_stays_quiet_for_a_worker_that_returned_then_tidied_its_plan(db):
    """#2009: archiving settled steps after a return is cleanup, not an empty plan.

    The parent's step already holds the returned work for its verdict, so the
    worker is not what the parent is waiting on and "returned nothing" would
    tell the parent to re-brief a finished worker.
    """
    from automation import plan_liveness
    from plans import archival, delegation

    step, _plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    submit_review_evidence(node.id, db=db, actor_id="worker")
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [step.id]
    returned = len(wakes(db))
    service.transition_node(
        node.id, "accepted", verification_result="passed",
        verified_by="parent", actor_id="parent", db=db,
    )
    archival.archive_node(node.id, actor_id="worker", db=db)

    plan_liveness_terminal.on_thread_turn_complete(
        "worker",
        db=db,
        effects=PendingTurnEndEffects(),
    )

    assert service.get_node(step.id, db=db).state == "review_requested"
    assert len(wakes(db)) == returned
