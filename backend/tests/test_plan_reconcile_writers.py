"""Corrections, review settlement and structure writers against reconciliation.

Task A holds its executor's slot and Task B is active and queued behind it.
A correction that makes A depend on new rework reaches `add_dependency`, which
releases A and reconciles in the same mutation. Review rejection and
acceptance with a follow-up go through the same release-then-reconcile path as
every settlement. Writers that leave A eligible - a watchdog-only edit, a split,
a standalone follow-up - never touch A's attempt and never select new pending
work.
"""

from __future__ import annotations

import pytest

from plans import review_evidence, service
from tests.plan_execution_helpers import add_task
from tests.test_plan_reconcile_release import (  # noqa: F401 - shared scaffolding
    attempt,
    db,
    open_attempt,
    plan_update,
    queued_pair,
    revision,
)


def _accepted_source(db, plan_id: str) -> str:
    """Select, review and accept one source task, leaving the slot empty."""
    source = add_task(db, plan_id, "Source work")
    plan_update(
        db, plan_id, "activate",
        {"node_id": source.id, "expected_revision": source.revision},
    )
    plan_update(
        db, plan_id, "request_review",
        {"node_id": source.id, "evidence": "Source proof passed",
         "expected_revision": revision(db, source.id)},
    )
    plan_update(
        db, plan_id, "accept",
        {"node_id": source.id, "verification_result": "passed",
         "expected_revision": revision(db, source.id)},
    )
    assert open_attempt(db, source.id) is None
    return source.id


def test_a_correction_blocking_the_selected_task_selects_the_queued_task(db):
    """`corrections.add` reconciles through `dependencies.add_dependency`."""
    plan = service.ensure_plan("thread", "owner", db=db)
    source = _accepted_source(db, plan.id)
    plan, task_a, a_attempt, task_b = queued_pair(db)

    corrected = plan_update(
        db, plan.id, "add_correction",
        {
            "node_id": source,
            "title": "Correct the source work",
            "acceptance_criteria": "The corrected source is verified",
            "affected_nodes": [
                {"node_id": task_a, "expected_revision": revision(db, task_a)},
            ],
            "expected_revision": revision(db, source),
        },
    )

    assert db.in_transaction is False
    assert corrected["correction"]["state"] == "pending"
    assert open_attempt(db, corrected["correction"]["id"]) is None
    assert attempt(db, a_attempt)["release_reason"] == "dependency_blocked"
    successor = open_attempt(db, task_b)
    assert successor is not None, "the correction left queued Task B unselected"
    assert successor["executor_thread_id"] == "owner"


def test_watchdog_only_edit_keeps_the_selected_attempt(db):
    """An edit that changes no requirement keeps a valid attempt."""
    plan, task_a, a_attempt, task_b = queued_pair(db)

    edited = plan_update(
        db, plan.id, "edit",
        {"node_id": task_a, "watchdog_budget_seconds": 900,
         "expected_revision": revision(db, task_a)},
    )

    assert edited["node"]["watchdog_budget_seconds"] == 900
    assert edited["node"]["requirements_revision"] == 1
    assert edited["admission"]["selection"]["attempt_id"] == a_attempt
    assert attempt(db, a_attempt)["released_at"] is None
    assert open_attempt(db, task_b) is None


def test_a_rejection_never_preempts_and_its_revision_waits_to_be_taken_up(db):
    """Reject releases the submission and reconciles without preempting a valid selection."""
    plan, task_a, a_attempt, task_b = queued_pair(db)
    plan_update(
        db, plan.id, "request_review",
        {"node_id": task_a, "evidence": "Focused proof passed",
         "expected_revision": revision(db, task_a)},
    )
    b_attempt = open_attempt(db, task_b)["id"]

    rejected = plan_update(
        db, plan.id, "reject",
        {"node_id": task_a, "feedback": "The proof misses the empty-input case",
         "expected_revision": revision(db, task_a)},
    )

    # The submission is settled; its revision is new pending work that preempts nothing.
    assert rejected["node"]["state"] == "closed"
    revision_id = rejected["revision"]["node_id"]
    assert revision_id != task_a
    assert open_attempt(db, task_b)["id"] == b_attempt
    assert open_attempt(db, revision_id) is None

    plan_update(
        db, plan.id, "fail",
        {"node_id": task_b, "expected_revision": revision(db, task_b)},
    )

    assert attempt(db, b_attempt)["release_reason"] == "failed"
    # Selection stays explicit: the revision is open work the executor takes up.
    assert open_attempt(db, revision_id) is None
    activated = plan_update(
        db, plan.id, "activate",
        {"node_id": revision_id, "expected_revision": revision(db, revision_id)},
    )
    assert activated["node"]["state"] == "active"
    assert open_attempt(db, revision_id)["attempt_ordinal"] == 1


def test_accept_with_follow_up_releases_and_selects_the_queued_task(db):
    """Accept with a follow-up releases the task and reconciles its plan.

    The service-level review writer leaves A's attempt open, so the release
    and reconcile below come from acceptance itself. The follow-up is pending
    work and is never selected.
    """
    plan, task_a, a_attempt, task_b = queued_pair(db)
    review_evidence.submit(
        task_a, evidence="Focused proof passed",
        expected_revision=revision(db, task_a), actor_id="owner",
        provenance="test.plan_reconcile", db=db,
    )
    db.commit()
    assert attempt(db, a_attempt)["released_at"] is None

    accepted = plan_update(
        db, plan.id, "accept_with_follow_up",
        {"node_id": task_a, "title": "Harden the edge case",
         "acceptance_criteria": "The edge case is covered",
         "verification_result": "passed", "expected_revision": revision(db, task_a)},
    )

    assert accepted["node"]["state"] == "accepted_with_followup"
    assert attempt(db, a_attempt)["release_reason"] == "accepted"
    assert open_attempt(db, task_b)["executor_thread_id"] == "owner"
    assert accepted["follow_up"]["state"] == "pending"
    assert open_attempt(db, accepted["follow_up"]["id"]) is None


@pytest.mark.parametrize("operation", ["split", "add_follow_up"])
def test_split_and_follow_up_leave_the_selection_unchanged(db, operation):
    """New pending nodes never preempt a selection or get selected."""
    plan, task_a, a_attempt, task_b = queued_pair(db)

    created = plan_update(
        db, plan.id, operation,
        {"node_id": task_a, "title": "Split-off work",
         "acceptance_criteria": "The split-off work is verified",
         "expected_revision": revision(db, task_a)},
    )

    new_node = created["node"] if operation == "split" else created["follow_up"]
    assert new_node["id"] not in {task_a, task_b}
    assert new_node["state"] == "pending"
    assert open_attempt(db, new_node["id"]) is None
    assert open_attempt(db, task_a)["id"] == a_attempt
    assert open_attempt(db, task_b) is None
