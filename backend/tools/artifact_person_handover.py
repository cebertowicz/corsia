"""Give a document to the signed-in person through the ownership policy."""

from infra import config, write_effects
from registry import models
from registry.document_supervisor import change_refusal
from registry.document_ownership_change import OwnershipBody, apply_change, emit_ownership_events
from registry.shared_principals import UI_PRINCIPAL, acting_owner, artifact_owner, document_project


NOT_THE_OWNER = "Ask the document's owner or their worker to give it to the person."
NO_PATH = "Only documents with a path can be given to the person."


def handover_to_person(db, routing, actor_thread_id: str, *, reason: str, causal_event_id: str | None, metadata: dict | None):
    identity = routing.get("identity")
    if identity is None or identity.get("state") != "active":
        return "not_found", None
    artifact = models.get_artifact(identity["id"], db=db)
    if artifact is None:
        return "not_found", None
    if not artifact.get("path") or artifact.get("type") != "document":
        return "person_error", NO_PATH
    current = artifact_owner(identity["id"], db=db)
    if current is None or current.startswith("project:"):
        # A project's own document, or an unowned one: refused in a project, and unowned outside one nobody hands over.
        _, project_id = document_project(artifact["path"], workspace=config.WORKSPACE, db=db)
        refusal = change_refusal(current, UI_PRINCIPAL, False, agent_in_project=project_id is not None or current is not None)
        return "person_error", refusal or NOT_THE_OWNER
    allowed = current != UI_PRINCIPAL and (
        actor_thread_id == current or acting_owner(actor_thread_id, False, db=db) == current
    )
    if not allowed:
        return "person_error", NOT_THE_OWNER
    body = OwnershipBody(owner="user")
    actor = db.execute("SELECT user_id FROM threads WHERE id=?", (actor_thread_id,)).fetchone()
    changed, events, rehome = apply_change(
        db, artifact, body, current, owner_user_id=actor["user_id"] if actor else None,
        actor_thread_id=actor_thread_id, authority="agent-handover-to-person",
        reason=reason, source_path="tool:artifact_rehome", causal_event_id=causal_event_id, metadata=metadata,
    )
    assert rehome is not None
    user_id = actor["user_id"] if actor else None
    write_effects.after_commit(db, lambda: emit_ownership_events(events, user_id))
    rehome["artifact"] = changed
    return "ok", rehome
