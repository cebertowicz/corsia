"""A row names a repeated attempt only when a try ended in failure.

Owner decision, 2026-09-16: the attempt subline shows friction only. An edit, a new
dependency, a watch, an attachment, a hold or a reselection opens another
attempt without anything having gone wrong, so none of them counts. Friction
is an attempt released for a failure reason (``commitment_cost.FAILED_RELEASES``)
that another attempt followed; a node that failed on its only try shows its
failure, not a second attempt. The count is over the whole history, not the
bounded list the row carries.
"""

from __future__ import annotations

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import (
    admission, commitment_cost, mutations, operator_node_state, operator_projection,
    rejection, review_evidence, service,
)
from plans.boundary import PlanActor

HUMAN = PlanActor(None, "user", None, kind="user")
PROVENANCE = "test.plan.attempt_friction"


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project','Project','t','t')"
    )
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,title,created_at,updated_at)
           VALUES ('owner','user','project','Plan owner','t','t')"""
    )
    db.commit()
    return db


def _running(db, title: str):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title=title,
        acceptance_criteria=f"{title} is proven", db=db,
    )
    with service._write(db):
        admission.activate(
            node.id, expected_revision=node.revision, actor_id="owner",
            provenance=PROVENANCE, db=db,
        )
    return node


def _retry(db, node_id: str, reason: str) -> None:
    """End the open attempt for ``reason`` and select the same task again."""
    with service._write(db):
        assert admission.release(node_id, reason, db=db)
        attempt, conflict = admission.select(
            node_id, actor_id="owner", provenance=PROVENANCE, db=db,
        )
    assert conflict is None and attempt is not None


def _row_ids(db) -> set[str]:
    entry = operator_projection.read_thread("owner", actor=HUMAN, db=db)["plans"][0]
    return {node["id"] for node in entry["nodes"]}


def _execution(db, node_id: str) -> dict:
    entry = operator_projection.read_thread("owner", actor=HUMAN, db=db)["plans"][0]
    return next(node for node in entry["nodes"] if node["id"] == node_id)["execution"]


def test_friction_is_the_one_shared_failure_release_set():
    assert commitment_cost.FAILED_RELEASES == frozenset({"failed", "review_rejected"})


@pytest.mark.parametrize("reason", sorted(commitment_cost.FAILED_RELEASES))
def test_a_retry_after_a_failed_try_counts_as_friction(reason):
    db = _db()
    try:
        node = _running(db, "Retried")
        _retry(db, node.id, reason)

        execution = _execution(db, node.id)
        assert execution["attempt_count"] == 2
        assert execution["friction_count"] == 1
    finally:
        db.close()


@pytest.mark.parametrize("reason", [
    "requirements_changed", "dependency_blocked", "watch_armed",
    "spawned_thread_attached", "paused", "blocked", "selection_invalidated",
    "plan_paused", "turn_terminal", "dispatch_settled", "review_requested",
])
def test_a_retry_for_any_other_reason_is_not_friction(reason):
    db = _db()
    try:
        node = _running(db, "Reselected")
        _retry(db, node.id, reason)

        execution = _execution(db, node.id)
        assert execution["attempt_count"] == 2
        assert execution["friction_count"] == 0
    finally:
        db.close()


def test_an_edit_only_second_attempt_is_not_friction():
    db = _db()
    try:
        node = _running(db, "Edited")
        mutations.edit_node(
            node.id, acceptance_criteria="Edited is proven twice",
            actor_id="owner", provenance=PROVENANCE, db=db,
        )

        execution = _execution(db, node.id)
        assert [a["release_reason"] for a in execution["attempts"]] == [
            "requirements_changed", None,
        ]
        assert execution["attempt_count"] == 2
        assert execution["friction_count"] == 0
    finally:
        db.close()


def test_a_task_that_failed_on_its_only_try_has_no_retry_to_name():
    db = _db()
    try:
        node = _running(db, "Failed once")
        with service._write(db):
            mutations.fail_node(node.id, actor_id="owner", provenance=PROVENANCE, db=db)
            assert admission.release(node.id, "failed", db=db)

        execution = _execution(db, node.id)
        assert execution["attempt_count"] == 1
        assert execution["friction_count"] == 0
    finally:
        db.close()


def test_friction_is_counted_past_the_bounded_history():
    db = _db()
    try:
        node = _running(db, "Long history")
        _retry(db, node.id, "failed")
        churn = operator_node_state.ATTEMPT_HISTORY_LIMIT + 3
        for _ in range(churn):
            _retry(db, node.id, "selection_invalidated")

        execution = _execution(db, node.id)
        assert execution["attempt_count"] == churn + 2
        assert len(execution["attempts"]) == operator_node_state.ATTEMPT_HISTORY_LIMIT
        assert all(a["release_reason"] != "failed" for a in execution["attempts"])
        assert execution["friction_count"] == 1
    finally:
        db.close()


def _rejected(db, node):
    """Submit this task for review and reject it; return the revision it made."""
    current = service.get_node(node.id, db=db)
    with service._write(db):
        review_evidence.submit(
            current.id, evidence=f"{current.title} is done",
            expected_revision=current.revision, actor_id="owner",
            provenance=PROVENANCE, db=db,
        )
    _rejected_node, revision, _replayed = rejection.reject_submission(
        node.id, feedback="Not yet", actor_id="owner", provenance=PROVENANCE, db=db,
    )
    with service._write(db):
        admission.release(node.id, "review_rejected", db=db)
    return revision


def test_a_rejections_revision_carries_the_retry_it_forced():
    db = _db()
    try:
        node = _running(db, "Rejected once")
        revision = _rejected(db, node)

        # The sent-back round is history of the task, not a row (#3011).
        assert node.id not in _row_ids(db)
        assert _execution(db, revision.id)["friction_count"] == 1
    finally:
        db.close()


def test_each_further_rejection_counts_on_the_newest_revision():
    db = _db()
    try:
        node = _running(db, "Rejected twice")
        first = _rejected(db, node)
        with service._write(db):
            admission.activate(
                first.id, expected_revision=first.revision, actor_id="owner",
                provenance=PROVENANCE, db=db,
            )
        second = _rejected(db, service.get_node(first.id, db=db))

        assert {node.id, first.id}.isdisjoint(_row_ids(db))
        assert _execution(db, second.id)["friction_count"] == 2
    finally:
        db.close()


def test_an_ordinary_follow_up_is_not_a_retry():
    db = _db()
    try:
        node = _running(db, "Accepted with more to do")
        follow_up = service.add_follow_up(
            node.id, title="The rest of it",
            acceptance_criteria="The rest is proven",
            actor_id="owner", provenance=PROVENANCE, db=db,
        )

        assert _execution(db, follow_up.id)["friction_count"] == 0
    finally:
        db.close()
