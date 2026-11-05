"""A PR's review starts from the one open task it is attached to (plans.public.linked_node_for_artifact)."""

from __future__ import annotations

import pytest

from infra.database import get_db
from plans import closure, service
from plans.public import link_node_artifact, linked_node_for_artifact
from registry import models
from registry.registration_writer import RegistrationOrigin
from tests.registry_helpers import make_session

PR = ("github", "pull-request", "cebertowicz/corsia#4242")


@pytest.fixture()
def db(isolated_db):
    return get_db()


def _active_task(db, plan_id: str, title: str):
    node = service.add_node(plan_id, kind="task", title=title, acceptance_criteria="Done", db=db)
    return service.transition_node(node.id, "active", db=db)


def _registered_from(db, thread_id: str, plan_id: str, node_id: str) -> str:
    """Register the PR during a Turn bound to ``node_id``, as artifact_register does."""
    turn_id = f"turn-{node_id}"
    commitment = service._node_row(db, node_id)["commitment_id"]
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
            plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role)
           VALUES (?,?,0,'native/test','running','t0','boot',?,?,?,?,'execution')""",
        (turn_id, thread_id, plan_id, node_id, f"attempt-{turn_id}", commitment),
    )
    pr = models.register_artifact(
        integration=PR[0], type=PR[1], external_id=PR[2], parent_thread_id=thread_id, title=PR[2],
        origin=RegistrationOrigin(thread_id, turn_id, "artifact_tool"), db=db,
    )
    db.commit()
    return pr["id"]


def _plan(db):
    thread_id = make_session(db, prefix="linked-node")
    return thread_id, service.ensure_plan("thread", thread_id, db=db)


def test_the_registering_task_is_the_one_open_node(db):
    thread_id, plan = _plan(db)
    task = _active_task(db, plan.id, "Open the PR")
    _registered_from(db, thread_id, plan.id, task.id)

    assert linked_node_for_artifact(*PR, db=db) == {"status": "ok", "node_id": task.id}


def test_a_settled_task_leaves_no_linked_task(db):
    thread_id, plan = _plan(db)
    task = _active_task(db, plan.id, "Open the PR")
    _registered_from(db, thread_id, plan.id, task.id)
    closure.close_node(task.id, reason="Not needed", db=db)
    db.commit()

    assert linked_node_for_artifact(*PR, db=db) == {"status": "no_linked_task", "node_id": None}


def test_a_result_link_counts_and_two_open_nodes_are_several(db):
    thread_id, plan = _plan(db)
    opener = _active_task(db, plan.id, "Open the PR")
    artifact_id = _registered_from(db, thread_id, plan.id, opener.id)
    closure.close_node(opener.id, reason="Handed on", db=db)
    finisher = _active_task(db, plan.id, "Finish the PR")
    link_node_artifact(finisher.id, artifact_id, "result", actor_id=None, provenance="test", db=db)
    db.commit()
    assert linked_node_for_artifact(*PR, db=db) == {"status": "ok", "node_id": finisher.id}

    other = _active_task(db, plan.id, "Review the PR")
    link_node_artifact(other.id, artifact_id, "result", actor_id=None, provenance="test", db=db)
    db.commit()
    assert linked_node_for_artifact(*PR, db=db) == {"status": "several_linked_tasks", "node_id": None}


def test_an_unknown_artifact_is_lookup_failed_and_a_failed_read_raises(db, monkeypatch):
    assert linked_node_for_artifact("github", "pull-request", "cebertowicz/corsia#1", db=db) == {
        "status": "lookup_failed", "node_id": None,
    }

    import sqlite3

    from registry import registration_events

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(registration_events, "routing_registrations", broken)
    with pytest.raises(sqlite3.OperationalError):
        linked_node_for_artifact(*PR, db=db)
