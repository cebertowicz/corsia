"""Release 2 failure and restart delegation journeys.

These scenarios share the real service wiring with the happy-path journeys in
``test_plan_delegation_journeys`` while keeping failure and replay coverage in
one focused, code-health-sized module.
"""

from __future__ import annotations

from automation.turn_end_effects import PendingTurnEndEffects
from automation import plan_liveness_terminal
from collections.abc import Iterator
import sqlite3

import pytest

from automation import plan_liveness
from plans import (
    closure,
    delegation,
    delegation_child_lifecycle,
    delegation_replace,
    mutations,
    notifications,
    service,
)
from plans.projections import liveness_signal
from tests.child_parent_notice_helpers import db, delegated, wakes
from tests.plan_delegation_journey_helpers import attach_child, wake_payload
from tests.plan_review_helpers import submit_review_evidence


# === Journey 3: failure and partial return ===================================


def test_journey3_a7_failing_the_last_step_returns_labeled_failed(db):
    """A7: the worker's last step failing is its return; the wake shows it failed."""
    step, _plan, node = delegated(db, title="Only step")
    service.transition_node(node.id, "active", actor_id="worker", db=db)

    mutations.fail_node(node.id, actor_id="worker", db=db)

    assert wakes(db) == []
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [step.id]
    appended = wakes(db)
    assert len(appended) == 1
    payload = wake_payload(appended[0])["returned"]
    assert payload["steps"][0]["state"] == "failed"
    assert service.get_node(step.id, db=db).state == "review_requested"


def test_journey3_a9_closing_the_last_step_returns_labeled_closed(db):
    """A9: the worker's last step closed is its return; the wake shows it closed.

    Closing archives the node in the same write (``plans/closure.py``), so no
    unarchived leaf is left to read. The return is then the plan's closed
    steps (``delegation_harvest.return_rows``): the worker's own Turn end
    harvests it directly (#2107), exactly once, and does not misfire the A11
    "returned nothing" wake.
    """
    step, _plan, node = delegated(db, title="Only step")

    closure.close_node(
        node.id,
        reason="Not needed after all",
        actor_id="worker",
        db=db,
    )
    plan_liveness_terminal.on_thread_turn_complete(
        "worker",
        db=db,
        effects=PendingTurnEndEffects(),
    )

    appended = wakes(db)
    assert len(appended) == 1
    payload = wake_payload(appended[0])["returned"]
    assert payload["steps"][0]["state"] == "closed"
    assert payload["steps"][0]["evidence"] == "Not needed after all"
    assert payload["total_count"] == 1
    assert service.get_node(step.id, db=db).state == "review_requested"

    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == []
    assert len(wakes(db)) == 1


def test_journey3_a_return_never_auto_accepts_the_parent_node(db):
    """Failed, closed and ordinary returns all land the parent step in review,
    never accepted -- the parent's own verdict is a separate act.
    """
    for title, act in (
        ("Fails", lambda node: mutations.fail_node(node.id, actor_id="worker", db=db)),
        (
            "Returns",
            lambda node: submit_review_evidence(node.id, db=db, actor_id="worker"),
        ),
        (
            "Closes",
            lambda node: closure.close_node(
                node.id,
                reason="No longer wanted",
                actor_id="worker",
                db=db,
            ),
        ),
    ):
        thread_id = f"worker-{title.lower()}"
        step, _plan, node = attach_child(db, thread_id, title=title)
        service.transition_node(node.id, "active", actor_id=thread_id, db=db)
        act(node)
        assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [step.id]
        assert service.get_node(step.id, db=db).state == "review_requested"


# === Journey 4: stopped, abandoned, or dead child ============================


def test_journey4_f1_turn_error_wakes_once_and_leaves_the_parent_free_to_replace(db):
    """F1: a dead child's step still names a worker, so the parent can replace it."""
    step, _plan, _node = delegated(db)

    plan_liveness_terminal.on_thread_turn_complete(
        "worker",
        outcome="error",
        error="session limit reached",
        turn_id="turn-1",
        clears_at="2026-09-19T00:00:00+00:00",
        db=db,
        effects=PendingTurnEndEffects(),
    )

    appended = wakes(db)
    assert len(appended) == 1
    assert "session limit reached" in str(appended[0]["content"])

    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,spawned_by,model,created_at,updated_at)
           VALUES ('worker-replacement','user-1','project-1','parent','native/model','t','t')"""
    )
    service.ensure_plan("thread", "worker-replacement", db=db)
    replacement = delegation_replace.replace_spawned_thread(
        step.id,
        "worker-replacement",
        actor_id="parent",
        provenance="test.journey4",
        db=db,
    )
    assert replacement.node.spawned_thread_id == "worker-replacement"
    assert replacement.replaced == "worker"


def test_journey4_f2_open_stop_wakes_once_and_stays_actionable(db):
    """F2: stopped with work open, even after its own stall notice; the parent can still fail the step."""
    step, _plan, _node = delegated(db)
    signal = liveness_signal("worker", db=db)
    plan_liveness.arm_plan_wake("worker", signal, cycle=1, db=db)

    for _ in range(2):
        plan_liveness_terminal.on_thread_turn_complete(
            "worker",
            db=db,
            effects=PendingTurnEndEffects(),
        )

    appended = wakes(db)
    assert len(appended) == 1
    assert "stopped with its work still open" in str(appended[0]["content"])

    mutations.fail_node(step.id, actor_id="parent", db=db)
    assert service.get_node(step.id, db=db).state == "failed"


def test_journey4_f3_purge_wakes_once_and_releases_the_attachment_to_redelegate(db):
    """F3: purge releases the attachment so the parent can re-delegate cleanly."""
    step, _plan, _node = delegated(db)

    delegation_child_lifecycle.on_child_thread_ended(db, "worker", reason="purged")

    appended = wakes(db)
    assert len(appended) == 1
    assert "purged" in str(appended[0]["content"]).lower()
    assert service.get_node(step.id, db=db).spawned_thread_id is None

    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,spawned_by,model,created_at,updated_at)
           VALUES ('worker-2','user-1','project-1','parent','native/model','t','t')"""
    )
    service.ensure_plan("thread", "worker-2", db=db)
    re_delegated = mutations.attach_spawned_thread(step.id, "worker-2", db=db)
    assert re_delegated.spawned_thread_id == "worker-2"


# === Journey 5: restart and idempotency ======================================


@pytest.fixture
def restart_db(tmp_path) -> Iterator[dict]:
    """Provide a file-backed lineage whose reopened connection proves persistence."""
    from infra.schema import SCHEMA_SQL

    path = tmp_path / "restart.db"

    def _open() -> sqlite3.Connection:
        connection = sqlite3.connect(str(path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    connection = _open()
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    for thread_id, spawned_by in (("parent", None), ("worker", "parent")):
        connection.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1','project-1',?,'native/model','t','t')""",
            (thread_id, spawned_by),
        )
    connection.commit()
    yield {"open": _open, "connection": connection}
    connection.close()


def _make_restart_child(connection, thread_id: str, title: str):
    connection.execute(
        """INSERT INTO threads
           (id,user_id,project_id,spawned_by,model,created_at,updated_at)
           VALUES (?,'user-1','project-1','parent','native/model','t','t')""",
        (thread_id,),
    )
    parent_plan = service.ensure_plan("thread", "parent", db=connection)
    step = service.add_node(
        parent_plan.id,
        kind="task",
        title=title,
        acceptance_criteria=f"{title} result is reviewed",
        spawn_kind="claude-code",
        db=connection,
    )
    service.transition_node(step.id, "active", db=connection)
    child_plan = service.ensure_plan("thread", thread_id, db=connection)
    node = service.add_node(
        child_plan.id,
        kind="task",
        title=f"{title} step",
        acceptance_criteria="Tests pass",
        db=connection,
    )
    mutations.attach_spawned_thread(step.id, thread_id, db=connection)
    return step, node


def _apply_restart_writers(connection):
    returned_step, returned_node = _make_restart_child(
        connection, "worker-returned", "Returns cleanly"
    )
    errored_step, _ = _make_restart_child(connection, "worker-errored", "Errors out")
    purged_step, _ = _make_restart_child(connection, "worker-purged", "Gets purged")
    service.transition_node(
        returned_node.id, "active", actor_id="worker-returned", db=connection
    )
    submit_review_evidence(returned_node.id, db=connection, actor_id="worker-returned")
    effects = PendingTurnEndEffects()
    harvested = delegation.harvest_ready_children(
        db=connection,
        drives=effects.drives,
        changed_plan_ids=effects.changed_plan_ids,
    )
    plan_liveness_terminal.on_thread_turn_complete(
        "worker-errored",
        outcome="error",
        error="crash",
        turn_id="turn-1",
        db=connection,
        effects=effects,
    )
    delegation_child_lifecycle.on_child_thread_ended(
        connection, "worker-purged", reason="purged"
    )
    connection.commit()
    notifications.notify_after_commit(effects.changed_plan_ids, db=connection)
    step_ids = (returned_step.id, errored_step.id, purged_step.id)
    before_ids = {row["id"] for row in wakes(connection)}
    return returned_step.id, harvested, step_ids, before_ids


def _replay_restart_writers(connection, step_ids):
    effects = PendingTurnEndEffects()
    harvested = delegation.harvest_ready_children(
        db=connection,
        drives=effects.drives,
        changed_plan_ids=effects.changed_plan_ids,
    )
    plan_liveness_terminal.on_thread_turn_complete(
        "worker-errored",
        outcome="error",
        error="crash",
        turn_id="turn-1",
        db=connection,
        effects=effects,
    )
    delegation_child_lifecycle.on_child_thread_ended(
        connection, "worker-purged", reason="purged"
    )
    connection.commit()
    notifications.notify_after_commit(effects.changed_plan_ids, db=connection)
    after_ids = {row["id"] for row in wakes(connection)}
    states = [service.get_node(step_id, db=connection).state for step_id in step_ids]
    return harvested, after_ids, states


def test_journey5_restart_replays_harvest_f1_and_f3_writers_without_a_second_wake(
    restart_db,
):
    """Rebuild the service state from the DB and replay every writer once more.

    Covers a child's ordinary return (harvest), F1 (Turn error) and F3
    (purge) on three separate children in one plan, then simulates a
    restart (fresh connection, same file) and replays each exact writer
    call again, proving none produces a second wake.
    """
    connection = restart_db["connection"]
    returned_step_id, harvested, step_ids, before_ids = _apply_restart_writers(
        connection
    )
    assert harvested == [returned_step_id]
    assert len(before_ids) == 3
    connection.close()

    fresh = restart_db["open"]()
    try:
        replayed, after_ids, states = _replay_restart_writers(fresh, step_ids)
        assert replayed == []
        assert after_ids == before_ids
        for state in states:
            assert state in ("review_requested", "active", "closed")
    finally:
        fresh.close()
