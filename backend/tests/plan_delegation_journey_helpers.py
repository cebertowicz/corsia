"""Shared helpers for delegation journey scenarios."""

from __future__ import annotations

import json

from plans import artifact_links, mutations, service


def attach_child(db, thread_id: str, *, title: str = "Delegate work"):
    """Create and attach one child thread under the shared parent."""
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,spawned_by,model,created_at,updated_at)
           VALUES (?,'user-1','project-1','parent','native/model','t','t')""",
        (thread_id,),
    )
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    step = service.add_node(
        parent_plan.id,
        kind="task",
        title=title,
        acceptance_criteria=f"{title} result is reviewed",
        spawn_kind="claude-code",
        db=db,
    )
    service.transition_node(step.id, "active", db=db)
    child_plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(
        child_plan.id,
        kind="task",
        title=f"{title} step",
        acceptance_criteria="Tests pass",
        db=db,
    )
    mutations.attach_spawned_thread(step.id, thread_id, db=db)
    return step, child_plan, node


def wake_payload(row) -> dict:
    """Decode a plan wake's structured payload."""
    _line, blob = str(row["content"]).split("\n", 1)
    return json.loads(blob)


def link_artifact(db, node_id: str, artifact_id: str, *, actor_id: str) -> None:
    """Register and pre-link one result artifact as evidence submission does."""
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,state,created_at,updated_at)
           VALUES (?,'test','document',?,'active','t','t')""",
        (artifact_id, artifact_id),
    )
    artifact_links.link(
        node_id,
        artifact_id,
        "result",
        actor_id=actor_id,
        provenance="test.journey",
        db=db,
    )
