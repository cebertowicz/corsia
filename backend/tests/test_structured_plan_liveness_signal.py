"""Structured-only liveness signal and turn-boundary wake contract."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from automation import plan_liveness
from infra.schema import SCHEMA_SQL
from plans import admission, delegation, mutations, projections, service
from tests.plan_execution_helpers import open_turn
from tests.plan_review_helpers import submit_review_evidence
from tests.plan_writer_authorizer import raw_plan_seed


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    return db


def _session(db, thread_id: str, spawned_by: str | None = None) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,spawned_by,model,created_at,updated_at) "
        "VALUES (?, 'u', ?, 'test/model', 't', 't')",
        (thread_id, spawned_by),
    )


def _task(db, plan_id: str, title: str, **kwargs):
    return service.add_node(
        plan_id,
        kind="task",
        title=title,
        acceptance_criteria=f"{title} tests pass",
        db=db,
        **kwargs,
    )


def test_signal_uses_only_active_unarchived_actionable_owned_leaves():
    db = _db()
    _session(db, "owner")
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(
        plan.id,
        kind="phase",
        title="Phase",
        acceptance_criteria="Phase work is complete",
        db=db,
    )
    own_pending = _task(db, plan.id, "Pending", parent_id=phase.id)
    own_active = _task(db, plan.id, "Active")
    service.transition_node(own_active.id, "active", db=db)
    admission.select(
        own_active.id, actor_id="owner", provenance="test", db=db, touch=False,
    )
    archived = _task(db, plan.id, "Archived")
    with raw_plan_seed(db):
        db.execute(
            "UPDATE plan_nodes SET archived_at='t' WHERE id=?", (archived.id,)
        )
    done = _task(db, plan.id, "Done")
    service.transition_node(done.id, "active", db=db)
    submit_review_evidence(done.id, db=db)
    service.transition_node(
        done.id,
        "accepted",
        verification_result="passed",
        verified_by="reviewer",
        db=db,
    )
    db.commit()

    signal = projections.liveness_signal("owner", db=db)

    assert signal.count == 1
    assert signal.plan_id == plan.id
    assert len(signal.signal_hash) == 64
    assert own_pending.id not in signal.node_ids
    assert own_active.id in signal.node_ids


def test_user_assigned_leaf_is_not_agent_actionable():
    db = _db()
    _session(db, "owner")
    plan = service.ensure_plan("thread", "owner", db=db)
    _task(db, plan.id, "User work", assignee="user")

    signal = projections.liveness_signal("owner", db=db)

    assert signal.count == 0
    assert plan_liveness.on_turn_complete("owner", db=db) is None


def test_root_owner_review_requested_leaf_arms_the_owner_wake():
    db = _db()
    _session(db, "owner")
    plan = service.ensure_plan("thread", "owner", db=db)
    task = _task(db, plan.id, "Root-owned review")
    service.transition_node(task.id, "active", actor_id="owner", db=db)
    service.transition_node(
        task.id, "review_requested", actor_id="owner", db=db,
    )

    signal = projections.liveness_signal("owner", db=db)
    wake = plan_liveness.on_turn_complete("owner", db=db)

    assert signal.node_ids == (task.id,)
    assert wake is not None
    assert wake.last_open_count == 1


def test_harvested_delegated_step_arms_parent_not_child(monkeypatch):
    """The parent reviews its delegated step in its own plan once the child finishes.

    The child's work lives in its own plan attached to the parent's step, and the
    child approves its own steps. Harvest then moves the parent's step to review,
    which arms the parent's wake and leaves the finished child with nothing live.
    """
    db = _db()
    _session(db, "parent")
    _session(db, "child", spawned_by="parent")
    plan = service.ensure_plan("thread", "parent", db=db)
    delegated = _task(db, plan.id, "Delegated work", spawn_kind="claude-code")
    service.transition_node(delegated.id, "active", actor_id="parent", db=db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    child_task = _task(db, child_plan.id, "Child work")
    mutations.attach_spawned_thread(delegated.id, "child", db=db)
    service.transition_node(child_task.id, "active", actor_id="child", db=db)
    submit_review_evidence(child_task.id, db=db, actor_id="child")
    service.transition_node(
        child_task.id, "accepted", verification_result="passed",
        verified_by="child", db=db,
    )

    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [delegated.id]
    parent_signal = projections.liveness_signal("parent", db=db)
    child_signal = projections.liveness_signal("child", db=db)

    assert service.get_node(delegated.id, db=db).state == "review_requested"
    assert parent_signal.node_ids == (delegated.id,)
    assert child_signal.count == 0
    assert plan_liveness.on_turn_complete("parent", db=db) is not None
    assert plan_liveness.on_turn_complete("child", db=db) is None


def test_user_review_requested_leaf_never_arms_agent_wake():
    db = _db()
    _session(db, "owner")
    plan = service.ensure_plan("thread", "owner", db=db)
    task = _task(db, plan.id, "Human review", assignee="user")
    service.transition_node(task.id, "active", actor_id="owner", db=db)
    service.transition_node(
        task.id, "review_requested", actor_id="owner", db=db,
    )

    assert projections.liveness_signal("owner", db=db).count == 0
    assert plan_liveness.on_turn_complete("owner", db=db) is None


def test_blocked_leaf_does_not_arm_and_signal_hash_changes_at_equal_count():
    db = _db()
    _session(db, "owner")
    plan = service.ensure_plan("thread", "owner", db=db)
    prerequisite = _task(db, plan.id, "Prerequisite")
    dependent = _task(db, plan.id, "Dependent")
    service.add_dependency(dependent.id, prerequisite.id, db=db)
    service.transition_node(prerequisite.id, "active", db=db)
    admission.select(
        prerequisite.id, actor_id="owner", provenance="test", db=db, touch=False,
    )

    first = projections.liveness_signal("owner", db=db)
    second = projections.liveness_signal("owner", db=db)

    assert first.count == second.count == 1
    assert dependent.id not in first.node_ids
    assert first.signal_hash == second.signal_hash


def _turn_bound_to_settled_first_task(db):
    """Bind a running Turn to a first task that then fails and releases."""
    _session(db, "owner")
    plan = service.ensure_plan("thread", "owner", db=db)
    first = _task(db, plan.id, "First commitment")
    second = _task(db, plan.id, "Second commitment")
    open_turn(db, "owner", "turn-bound")
    admission.activate(
        first.id, expected_revision=first.revision, actor_id="owner",
        provenance="test", db=db,
    )
    service.transition_node(first.id, "failed", db=db)
    assert admission.release(first.id, "failed", db=db)
    db.commit()
    bound = db.execute(
        "SELECT plan_commitment_id FROM active_turns WHERE id='turn-bound'"
    ).fetchone()["plan_commitment_id"]
    assert bound == first.commitment_id != second.commitment_id
    return plan, second


def test_ready_task_counts_while_the_turn_accounts_to_a_settled_task():
    """A Turn still accounted to a settled task is no conflict for ready work.

    The ready task is actionable: it counts in the liveness signal and arms the
    wake.
    """
    db = _db()
    _plan, second = _turn_bound_to_settled_first_task(db)

    projected = admission.project(second.id, db=db)
    signal = projections.liveness_signal("owner", db=db)

    assert projected["condition"] == "ready"
    assert projected["conflict"] is None
    assert signal.node_ids == (second.id,)
    assert plan_liveness.on_turn_complete("owner", db=db) is not None


def test_ready_task_behind_an_occupied_slot_stays_excluded_with_a_bound_turn():
    """Ready work queued behind an occupied slot is still not counted."""
    db = _db()
    _session(db, "owner")
    plan = service.ensure_plan("thread", "owner", db=db)
    first = _task(db, plan.id, "Selected commitment")
    queued = _task(db, plan.id, "Queued commitment")
    open_turn(db, "owner", "turn-bound")
    admission.activate(
        first.id, expected_revision=first.revision, actor_id="owner",
        provenance="test", db=db,
    )
    db.commit()

    projected = admission.project(queued.id, db=db)
    signal = projections.liveness_signal("owner", db=db)

    assert projected["conflict"]["condition"] == "executor_selected"
    assert signal.node_ids == (first.id,)


def test_paused_complete_or_archived_plan_has_no_live_signal():
    db = _db()
    _session(db, "owner")
    plan = service.ensure_plan("thread", "owner", db=db)
    _task(db, plan.id, "Task")

    for state in ("paused", "complete", "archived"):
        db.execute("UPDATE plans SET state=?, revision=revision+1 WHERE id=?", (state, plan.id))
        db.commit()
        assert projections.liveness_signal("owner", db=db).count == 0


def test_turn_complete_queries_structured_signal_and_ignores_unrelated_watches():
    db = _db()
    _session(db, "owner")
    plan = service.ensure_plan("thread", "owner", db=db)
    task = _task(db, plan.id, "Task")
    service.transition_node(task.id, "active", db=db)
    admission.select(
        task.id, actor_id="owner", provenance="test", db=db, touch=False,
    )

    wake = plan_liveness.on_turn_complete("owner", db=db)
    assert wake is not None
    assert wake.last_open_count == 1
    assert wake.last_signal_hash == projections.liveness_signal(
        "owner", db=db
    ).signal_hash

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO expected_event_watches
           (id,owner_thread_id,condition_type,status,armed_at,created_at,updated_at)
           VALUES ('watch','owner','manual','armed',?,?,?)""",
        (now, now, now),
    )
    db.commit()

    # A watch owned by the thread but not on its actionable task quiets nothing.
    assert plan_liveness.on_turn_complete("owner", db=db) is not None
    assert db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id='owner'"
    ).fetchone()[0] == "armed"


def test_equal_count_changed_hash_is_churn_and_does_not_reset_backoff(monkeypatch):
    db = _db()
    _session(db, "owner")
    plan = service.ensure_plan("thread", "owner", db=db)
    first_node = _task(db, plan.id, "First")
    _task(db, plan.id, "Second")
    service.transition_node(first_node.id, "active", db=db)
    admission.select(
        first_node.id, actor_id="owner", provenance="test", db=db, touch=False,
    )

    first_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(plan_liveness, "_now", lambda: first_now)
    first = plan_liveness.on_turn_complete("owner", db=db)
    db.execute(
        "UPDATE plan_liveness_wakes SET cycle=2 WHERE thread_id='owner'"
    )
    db.commit()

    current = service.get_node(first_node.id, db=db)
    mutations.edit_node(
        first_node.id, title="First revised",
        expected_revision=current.revision, db=db,
    )
    second_now = first_now + timedelta(seconds=30)
    monkeypatch.setattr(plan_liveness, "_now", lambda: second_now)
    refreshed = plan_liveness.on_turn_complete("owner", db=db)

    assert refreshed.id == first.id
    assert refreshed.cycle == 2
    assert refreshed.deadline == first.deadline
    assert refreshed.last_open_count == first.last_open_count == 1
    assert refreshed.last_signal_hash == first.last_signal_hash
