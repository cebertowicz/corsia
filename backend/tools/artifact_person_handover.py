"""Give a document to the signed-in person through the ownership policy."""

from infra import config
from registry import models
from registry.document_ownership_change import OwnershipBody, apply_change, refusal_for
from registry.document_supervisor import is_multi_person, owner_kind
from registry.shared_principals import UI_PRINCIPAL, artifact_owner, document_project, scope_conductor


def _owner_or_worker(db, actor: str, owner: str) -> bool:
    """Follow only temporary-worker links; another persistent agent is not a worker."""
    seen = set()
    while actor and actor not in seen:
        if actor == owner:
            return True
        seen.add(actor)
        row = db.execute("SELECT spawned_by, agent_enabled FROM threads WHERE id=?", (actor,)).fetchone()
        if row is None or row["agent_enabled"]:
            return False
        actor = row["spawned_by"]
    return False


def handover_to_person(db, routing, actor_thread_id: str, *, reason: str, causal_event_id: str | None, metadata: dict | None):
    identity = routing.get("identity")
    if identity is None or identity.get("state") != "active":
        return "not_found", None
    artifact = models.get_artifact(identity["id"], db=db)
    if artifact is None or not artifact.get("path") or artifact.get("type") != "document":
        return "not_found", None
    current = artifact_owner(identity["id"], db=db)
    if current is None or (current is not None and current.startswith("project:")):
        _, project_id = document_project(artifact["path"], workspace=config.WORKSPACE, db=db)
        allowed = (
            project_id is not None
            and (current is None or current == f"project:{project_id}")
            and scope_conductor(project_id, db=db) == actor_thread_id
        )
    else:
        allowed = current != UI_PRINCIPAL and _owner_or_worker(db, actor_thread_id, current)
    if not allowed:
        return "person_forbidden", None
    body = OwnershipBody(owner="user")
    refusal = refusal_for(body, current, is_multi_person(db), set(), agent_released=current is None)
    if refusal:
        return "person_refused", refusal[1]
    actor = db.execute("SELECT user_id FROM threads WHERE id=?", (actor_thread_id,)).fetchone()
    changed, _events, rehome = apply_change(
        db, artifact, body, current, owner_user_id=actor["user_id"] if actor else None,
        actor_thread_id=actor_thread_id, authority="agent-handover-to-person",
        reason=reason, source_path="tool:artifact_rehome", causal_event_id=causal_event_id, metadata=metadata,
    )
    assert rehome is not None
    rehome["artifact"] = changed
    return "ok", rehome
