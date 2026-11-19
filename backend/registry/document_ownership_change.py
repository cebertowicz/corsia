"""Shared rules and transaction for document owner and supervisor changes."""

from pydantic import BaseModel

from registry import models
from registry.document_supervisor import SUPERVISOR_KEY, change_refusal, owner_kind, stored_pick
from registry.identity_metadata import merge_identity_metadata
from registry.shared_principals import UI_PRINCIPAL


class OwnershipBody(BaseModel):
    """Fields left out stay as they are."""

    owner: str | None = None
    supervisor_thread_id: str | None = None


AGENT_CHOICE_REFUSAL = "Pick one of the agents listed."
PERSON_OR_AGENT_CHOICE_REFUSAL = "Pick yourself or one of the agents listed."


def emit_ownership_events(events: list, user_id: str | None) -> None:
    """Emit the same ownership measurements for each committed change path."""
    from infra.product_events import emit_product_event

    for action, properties in events:
        emit_product_event("documents", action, user_id=user_id, properties=properties)


def refusal_for(body: OwnershipBody, current: str | None, multi_person: bool, agents: set[str]) -> tuple[int, str] | None:
    """The status and sentence this change is refused with, or ``None``.

    ``agents`` are the payload's choices, the only agents a pick may name. Only
    a new owner is checked against them; one that stays may be outside them.
    """
    new_owner = current if body.owner is None else UI_PRINCIPAL if body.owner == "user" else body.owner
    shared_refusal = change_refusal(current, new_owner, multi_person) if body.owner is not None else None
    if shared_refusal:
        return 409, shared_refusal
    if body.owner not in (None, "user") and new_owner != current and new_owner not in agents:
        return 400, PERSON_OR_AGENT_CHOICE_REFUSAL
    if body.supervisor_thread_id and body.supervisor_thread_id not in agents:
        return 400, AGENT_CHOICE_REFUSAL
    return None


def apply_change(db, artifact: dict, body: OwnershipBody, current: str | None, *, owner_user_id: str | None = None, actor_thread_id: str | None = None, authority: str = "signed-in-person", reason: str = "changed by the signed-in person", source_path: str = "api:/api/artifacts/{id}/ownership", causal_event_id: str | None = None, metadata: dict | None = None) -> tuple[dict, list, dict | None]:
    """Make the change inside the caller's transaction and return the changed artifact and events.

    Every comparison is against the stored pick, the one value the header and
    comment delivery read, so the person's explicit choice is always written and
    an event fires only when what is stored changes.
    """
    artifact_id = artifact["id"]
    stored = stored_pick(artifact)
    new_owner = current if body.owner is None else UI_PRINCIPAL if body.owner == "user" else body.owner
    picked = "supervisor_thread_id" in body.model_fields_set
    supervisor = (body.supervisor_thread_id or None) if picked else stored
    # The agent a person takes a document from supervises it, unless they picked one.
    if new_owner == UI_PRINCIPAL and current != UI_PRINCIPAL and supervisor is None and owner_kind(current) == "agent":
        supervisor = current
    rehome = None
    if new_owner != current:
        rehome = models.rehome_artifact(
            integration=artifact["integration"], type=artifact["type"], external_id=artifact["external_id"],
            new_owner_thread_id=new_owner, actor_thread_id=actor_thread_id, authority=authority,
            reason=reason, source_path=source_path, causal_event_id=causal_event_id, metadata=metadata, db=db,
        )
    if body.owner == "user" and owner_user_id is not None:
        merge_identity_metadata(db, artifact_id, {"owner_user_id": owner_user_id})
    elif body.owner is not None and body.owner != "user":
        merge_identity_metadata(db, artifact_id, {"owner_user_id": None})
    # The owner supervises by default, so picking the owner stores no pick.
    supervisor = supervisor if supervisor != new_owner else None
    events = []
    if new_owner != current:
        events.append(("owner_changed", {"from": owner_kind(current), "to": owner_kind(new_owner)}))
    if supervisor != stored:
        merge_identity_metadata(db, artifact_id, {SUPERVISOR_KEY: supervisor})
        events.append(("supervisor_changed", {"from": "agent" if stored else "none", "to": "agent" if supervisor else "none"}))
    return models.get_artifact(artifact_id, db=db), events, rehome
