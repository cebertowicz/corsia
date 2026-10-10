"""Focused contract tests for the P1 structured-plan service."""

from __future__ import annotations

from collections.abc import Iterator

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import review_evidence, service


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    yield connection
    connection.close()


def _task(plan_id: str, title: str, *, db: sqlite3.Connection, **kwargs):
    acceptance_criteria = kwargs.pop("acceptance_criteria", "Focused tests pass")
    return service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=acceptance_criteria, db=db, **kwargs
    )


def _request_review(node_id: str, *, db: sqlite3.Connection):
    current = service.get_node(node_id, db=db)
    return review_evidence.submit(
        node_id,
        evidence="Focused proof passed",
        expected_revision=current.revision,
        actor_id="worker",
        provenance="test.structured_plans",
        db=db,
    )


def _review_and_accept(node_id: str, *, db: sqlite3.Connection) -> None:
    service.transition_node(node_id, "active", db=db)
    _request_review(node_id, db=db)
    service.transition_node(
        node_id, "accepted", verification_result="passed", verified_by="review-thread", db=db
    )


class _CancellationSignal(BaseException):
    """Test-only cancellation that exercises BaseException rollback paths."""


def test_ensure_plan_is_owner_unique_and_persists_scope_identity(db):
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    first = service.ensure_plan("thread", "thread-1", db=db)
    again = service.ensure_plan("thread", "thread-1", db=db)
    scope = service.ensure_plan("project_scope", "project-1", db=db)

    assert again == first
    assert scope.owner_id == "project-1"
    workspace = db.execute(
        "SELECT id FROM plans WHERE owner_kind='workspace_scope'"
    ).fetchone()
    assert workspace is not None
    assert service._plan_row(db, scope.id)["parent_plan_id"] == workspace["id"]
    assert db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 3


def test_node_requires_dod_and_checkpoint_names_resulting_plan_expansion(db):
    plan = service.ensure_plan("thread", "thread-1", db=db)

    with pytest.raises(service.PlanError, match="acceptance criteria"):
        _task(plan.id, "No DOD", db=db, acceptance_criteria=" ")
    with pytest.raises(service.PlanError, match="resulting plan expansion"):
        _task(plan.id, "Checkpoint", db=db, checkpoint_exception=True)

    checkpoint = _task(
        plan.id, "Checkpoint", db=db, checkpoint_exception=True,
        acceptance_criteria="Deliver the resulting plan expansion with child nodes",
    )
    assert checkpoint.checkpoint_exception is True


def test_write_rolls_back_base_exception_for_owned_and_nested_transactions(db):
    db.execute("CREATE TABLE plan_write_probe (value TEXT)")

    with pytest.raises(_CancellationSignal):
        with service._write(db):
            db.execute("INSERT INTO plan_write_probe VALUES ('owned')")
            raise _CancellationSignal
    assert db.execute("SELECT COUNT(*) FROM plan_write_probe").fetchone()[0] == 0
    assert db.in_transaction is False

    db.execute("BEGIN")
    with pytest.raises(_CancellationSignal):
        with service._write(db):
            db.execute("INSERT INTO plan_write_probe VALUES ('nested')")
            raise _CancellationSignal
    assert db.execute("SELECT COUNT(*) FROM plan_write_probe").fetchone()[0] == 0
    assert db.in_transaction is True
    db.rollback()


def test_tree_legal_nesting_order_and_cross_plan_parent_rejection(db):
    plan = service.ensure_plan("thread", "thread-1", db=db)
    other = service.ensure_plan("thread", "thread-2", db=db)
    phase = service.add_node(plan.id, kind="phase", title="Phase", acceptance_criteria="Phase DOD", db=db)
    track = service.add_node(
        plan.id, kind="track", title="Track", acceptance_criteria="Track DOD", parent_id=phase.id, db=db
    )
    task = _task(plan.id, "Task", parent_id=track.id, db=db)

    with pytest.raises(service.PlanError, match="leaves"):
        _task(plan.id, "Invalid child", parent_id=task.id, db=db)
    with pytest.raises(service.PlanError, match="another plan"):
        _task(other.id, "Invalid parent", parent_id=phase.id, db=db)

    first = _task(plan.id, "First", db=db)
    second = _task(plan.id, "Second", db=db, order_key=0)
    assert [row["title"] for row in db.execute(
        "SELECT title FROM plan_nodes WHERE plan_id=? AND parent_id IS NULL ORDER BY order_key", (plan.id,)
    )] == ["Second", "Phase", "First"]
    assert service.reorder_node(first.id, 0, db=db).order_key == 0
    assert second.id != first.id


def test_dependencies_are_normalized_acyclic_and_derive_blocked_progress(db):
    plan = service.ensure_plan("thread", "thread-1", db=db)
    prerequisite = _task(plan.id, "Prerequisite", db=db)
    dependent = _task(plan.id, "Dependent", db=db)
    service.add_dependency(dependent.id, prerequisite.id, db=db)
    service.add_dependency(dependent.id, prerequisite.id, db=db)

    assert service.get_node(dependent.id, db=db).unmet_dependencies
    dependency_events = db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE node_id=? AND event='dependency_added'",
        (dependent.id,),
    ).fetchone()[0]
    assert dependency_events == 1
    # Derived blocked gates admission; the displayed liveness reads ordinary
    # sequencing behind open work as a neutral wait.
    assert service.progress(plan.id, db=db).liveness == "waiting"
    with pytest.raises(service.PlanError, match="cycle"):
        service.add_dependency(prerequisite.id, dependent.id, db=db)

    _review_and_accept(prerequisite.id, db=db)
    service.transition_node(dependent.id, "active", db=db)
    projection = service.progress(plan.id, db=db)
    # The accepted prerequisite is a finished top-level branch: it folds out.
    assert projection.done == 0
    assert projection.total == 1
    # Lifecycle-active without an admitted executor attempt is not advancing.
    assert projection.liveness == "idle"


def test_transition_legality_and_passed_verification_gate(db):
    plan = service.ensure_plan("thread", "thread-1", db=db)
    node = _task(plan.id, "Verify", db=db)

    with pytest.raises(service.PlanError, match="illegal"):
        service.transition_node(node.id, "accepted", db=db)
    service.transition_node(node.id, "active", db=db)
    _request_review(node.id, db=db)
    with pytest.raises(service.PlanError, match="passed verification result"):
        service.transition_node(
            node.id, "accepted", verification_result="failed", verified_by="reviewer-1", db=db
        )

    accepted = service.transition_node(
        node.id, "accepted", verification_result="passed", verified_by="reviewer-1", db=db
    )
    assert accepted.state == "accepted"
    with pytest.raises(service.PlanError, match="illegal"):
        service.transition_node(node.id, "active", db=db)


def test_node_rejects_invalid_assignee(db):
    plan = service.ensure_plan("thread", "thread-1", db=db)

    with pytest.raises(service.PlanError, match="assignee"):
        _task(plan.id, "Invalid assignee", assignee="agent", db=db)


def test_accept_with_follow_up_is_atomic_linked_and_unbounded_in_depth(db):
    plan = service.ensure_plan("thread", "thread-1", db=db)
    node = _task(plan.id, "Original", db=db)
    service.transition_node(node.id, "active", db=db)
    _request_review(node.id, db=db)

    accepted, follow_up = service.accept_with_follow_up(
        node.id, title="Follow up", acceptance_criteria="Follow-up DOD", verification_result="passed",
        verified_by="reviewer-1", db=db,
    )
    assert accepted.state == "accepted_with_followup"
    assert follow_up.follow_up_of == node.id
    second = service.add_follow_up(follow_up.id, title="Second", acceptance_criteria="Second DOD", db=db)
    third = service.add_follow_up(second.id, title="Third", acceptance_criteria="Third DOD", db=db)
    fourth = service.add_follow_up(third.id, title="Fourth", acceptance_criteria="Fourth DOD", db=db)
    assert fourth.follow_up_of == third.id


def test_accept_with_follow_up_rolls_back_when_follow_up_dod_is_invalid(db):
    plan = service.ensure_plan("thread", "thread-1", db=db)
    node = _task(plan.id, "Original", db=db)
    service.transition_node(node.id, "active", db=db)
    _request_review(node.id, db=db)

    with pytest.raises(service.PlanError, match="acceptance criteria"):
        service.accept_with_follow_up(
            node.id, title="Invalid", acceptance_criteria=" ", verification_result="passed",
            verified_by="reviewer-1", db=db,
        )

    assert service.get_node(node.id, db=db).state == "review_requested"
    assert db.execute("SELECT COUNT(*) FROM plan_nodes WHERE follow_up_of=?", (node.id,)).fetchone()[0] == 0


def test_events_are_append_only_and_progress_distinguishes_review(db):
    plan = service.ensure_plan("thread", "thread-1", db=db)
    node = _task(plan.id, "Review", db=db)
    service.transition_node(node.id, "active", db=db)
    _request_review(node.id, db=db)

    assert service.progress(plan.id, db=db).liveness == "needs-review"
    event_id = db.execute("SELECT id FROM plan_node_events WHERE node_id=?", (node.id,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE plan_node_events SET event='mutated' WHERE id=?", (event_id,))
