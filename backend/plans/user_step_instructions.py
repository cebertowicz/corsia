"""Validate and project a person step's linked instructions document."""
from __future__ import annotations

from plans import node_states, service


def artifact_id(params: dict, actor, *, db) -> str | None:
    """Return the owned document requested at creation, or refuse its bad shape."""
    value = params.get("instructions_artifact_id")
    if value is None:
        return None
    if params.get("kind", "task") != "task" or params.get("assignee") != "user":
        raise service.PlanInputError(
            "instructions_artifact_id is only valid for an assignee: user task"
        )
    if not isinstance(value, str) or not value.strip():
        raise service.PlanInputError("instructions_artifact_id must name a registered document")
    artifact_id = value.strip()
    identity = db.execute(
        "SELECT type,state FROM artifact_identities WHERE id=?", (artifact_id,)
    ).fetchone()
    if identity is None or identity["state"] != "active":
        raise service.PlanInputError(
            f"instructions_artifact_id {artifact_id!r} names no registered document"
        )
    if identity["type"] != "document":
        raise service.PlanInputError(
            f"instructions_artifact_id must name a registered document, not {identity['type']!r}"
        )
    from plans import artifact_links

    _known, owner = artifact_links.identity_owner(artifact_id, db=db)
    if actor.kind != "thread" or owner != actor.thread_id:
        raise service.PlanInputError(
            "instructions_artifact_id must name a document this thread owns"
        )
    return artifact_id


def link_requested(node_id: str, params: dict, actor, *, db) -> None:
    """Validate and link the optional instructions document after node creation."""
    requested_id = artifact_id(params, actor, db=db)
    if requested_id is None:
        return
    from plans import artifact_links

    artifact_links.link(
        node_id, requested_id, "input", actor_id=actor.thread_id,
        provenance="plans.person_step_instructions", db=db,
    )


def read_for_artifact(artifact_id: str, *, db) -> dict | None:
    """Return the one open person step whose input is this instructions document."""
    row = db.execute(
        f"""SELECT n.id,n.title,n.revision,n.plan_id,p.owner_id,t.title AS owner_title
             FROM plan_node_artifact_links l
             CROSS JOIN plan_nodes n ON n.id=l.node_id
             CROSS JOIN plans p ON p.id=n.plan_id
             LEFT JOIN threads t ON t.id=p.owner_id
            WHERE l.artifact_id=? AND l.role='input'
              AND n.kind='task' AND n.assignee='user'
              AND {node_states.open_sql('n')}
            ORDER BY l.created_at DESC LIMIT 1""",
        (artifact_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "node_id": row["id"], "node_title": row["title"], "node_revision": row["revision"],
        "plan_id": row["plan_id"], "owner_thread_id": row["owner_id"],
        "owner_name": row["owner_title"] or "An agent",
    }
