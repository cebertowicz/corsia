"""Shared setup helpers for replacement-worker scenarios."""

from __future__ import annotations

import json

from plans import admission, service
from plans.boundary import PlanActor, mutate
from tests.child_parent_notice_helpers import notices

PARENT = PlanActor("parent", "user-1", "project-1")


def thread(db, thread_id: str, spawned_by: str) -> None:
    """Insert a spawned worker thread into the test lineage."""
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,spawned_by,model,created_at,updated_at)
           VALUES (?,'user-1','project-1',?,'native/model','t','t')""",
        (thread_id, spawned_by),
    )


def row(db, node_id: str):
    """Fetch one plan node row."""
    return db.execute("SELECT * FROM plan_nodes WHERE id=?", (node_id,)).fetchone()


def events(db, node_id: str, event: str) -> list[dict]:
    """Return decoded events of one kind for a node."""
    return [
        json.loads(item["payload_json"] or "{}")
        for item in db.execute(
            "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event=?"
            " ORDER BY rowid",
            (node_id, event),
        )
    ]


def started_replacement(db):
    """Create a replacement worker with its own active step."""
    thread(db, "replacement", "parent")
    plan = service.ensure_plan("thread", "replacement", db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title="Fix it properly",
        acceptance_criteria="Green",
        db=db,
    )
    admission.activate(
        node.id,
        expected_revision=node.revision,
        actor_id="replacement",
        provenance="test.replace",
        db=db,
    )
    return plan, node


def replace(db, step_id: str, thread_id: str):
    """Replace a parent step's worker through the public mutation boundary."""
    return mutate(
        service._node_row(db, step_id)["plan_id"],
        "replace_spawned_thread",
        {
            "node_id": step_id,
            "spawned_thread_id": thread_id,
            "expected_revision": row(db, step_id)["revision"],
        },
        actor=PARENT,
        provenance="test",
        mutation_id=f"replace-{thread_id}",
        db=db,
    ).result


def replaced_notices(db, thread_id: str) -> list:
    """Return replacement notifications for a worker."""
    return [
        item
        for item in notices(db, thread_id)
        if item["change_kind"] == "plan.worker_replaced"
    ]
