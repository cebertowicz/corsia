"""A2.5 explicit artifact ownership transfer/release endpoints.

Split out from ``api/registry.py`` so the generic register/resolve/pane surface
stays inside its code-health budget. The generic ``POST /register`` is
owner-preserving; these endpoints are the audited rescue/handoff/release path.
Authority is checked against the SOURCE scope (current owner when present, else
explicit triage, else the latest release event's old owner) — passive
subscribers never define transfer authority.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from infra.auth import authenticate, may_access_artifact, require_human_user
from infra.database import write_transaction
from infra.db_async import run_in_db
from infra.product_events import emit_product_event
from infra.queries import get_thread_for_user
from registry import models, service
from registry.ownership_authority import (
    can_rehome,
    can_release,
    rehome_source_thread_id,
    thread_is_wakeable,
)

router = APIRouter(prefix="/api")


class RehomeBody(BaseModel):
    integration: str
    type: str
    external_id: str
    new_owner_thread_id: str
    actor_thread_id: str
    authority: str
    reason: str
    source_path: str = "api:/api/artifacts/rehome"
    causal_event_id: str | None = None
    metadata: dict | None = None


class ReleaseBody(BaseModel):
    integration: str
    type: str
    external_id: str
    actor_thread_id: str
    authority: str
    reason: str
    source_path: str = "api:/api/artifacts/release"
    triage_thread_id: str | None = None
    causal_event_id: str | None = None
    metadata: dict | None = None


@router.post("/artifacts/rehome")
async def rehome_artifact(token: str, body: RehomeBody):
    """Explicitly transfer artifact ownership; generic register never does."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    if not (body.integration.strip() and body.type.strip() and body.external_id.strip()):
        raise HTTPException(400, "integration, type and external_id are required")
    if not body.reason.strip():
        raise HTTPException(400, "reason is required")

    def _rehome() -> tuple[str, dict | None]:
        with write_transaction(None, scope="api_rehome_artifact") as db:
            routing = models.routing_registrations(
                body.integration, body.type, body.external_id, db=db
            )
            owner = routing.get("owner")
            if owner is None and routing.get("identity") is None:
                return "not_found", None
            current_owner = owner["parent_thread_id"] if owner else None
            source_thread_id = rehome_source_thread_id(routing, db)
            source_thread_row = (
                get_thread_for_user(source_thread_id, user["id"], db=db)
                if source_thread_id else None
            )
            target_thread_row = get_thread_for_user(
                body.new_owner_thread_id, user["id"], db=db
            )
            actor_thread_row = get_thread_for_user(
                body.actor_thread_id, user["id"], db=db
            )
            if (actor_thread_row is None
                    or not thread_is_wakeable(target_thread_row)
                    or (source_thread_id and source_thread_row is None)):
                return "not_found", None
            if not can_rehome(
                authority=body.authority,
                actor_thread_id=body.actor_thread_id,
                current_owner_thread_id=current_owner,
                source_thread_row=source_thread_row,
                target_thread_row=target_thread_row,
                db=db,
            ):
                return "forbidden", {
                    "current_owner_thread_id": current_owner,
                    "requested_owner_thread_id": body.new_owner_thread_id,
                }
            return "ok", models.rehome_artifact(
                integration=body.integration,
                type=body.type,
                external_id=body.external_id,
                new_owner_thread_id=body.new_owner_thread_id,
                actor_thread_id=body.actor_thread_id,
                authority=body.authority,
                reason=body.reason,
                source_path=body.source_path,
                causal_event_id=body.causal_event_id,
                metadata=body.metadata,
                db=db,
            )

    status, result = await run_in_db(_rehome)
    if status == "not_found":
        raise HTTPException(404, "Artifact or thread not found")
    if status == "forbidden":
        raise HTTPException(403, result or "Re-home authority denied")
    assert result is not None
    service.notify_changed(result["artifact"])
    return result


@router.post("/artifacts/release")
async def release_artifact(token: str, body: ReleaseBody):
    """Explicitly clear artifact ownership; no implicit workspace fallback."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    if not (body.integration.strip() and body.type.strip() and body.external_id.strip()):
        raise HTTPException(400, "integration, type and external_id are required")
    if not body.reason.strip():
        raise HTTPException(400, "reason is required")

    def _release() -> tuple[str, dict | None]:
        with write_transaction(None, scope="api_release_artifact") as db:
            routing = models.routing_registrations(
                body.integration, body.type, body.external_id, db=db
            )
            owner = routing.get("owner")
            if owner is None:
                return "not_found", None
            current_owner = owner["parent_thread_id"]
            source_thread_row = get_thread_for_user(current_owner, user["id"], db=db)
            actor_thread_row = get_thread_for_user(
                body.actor_thread_id, user["id"], db=db
            )
            triage_thread_row = (
                get_thread_for_user(body.triage_thread_id, user["id"], db=db)
                if body.triage_thread_id else None
            )
            if source_thread_row is None or actor_thread_row is None:
                return "not_found", None
            if body.triage_thread_id and not thread_is_wakeable(triage_thread_row):
                return "not_found", None
            if not can_release(
                authority=body.authority,
                actor_thread_id=body.actor_thread_id,
                current_owner_thread_id=current_owner,
                source_thread_row=source_thread_row,
                triage_thread_row=triage_thread_row,
                db=db,
            ):
                return "forbidden", {
                    "current_owner_thread_id": current_owner,
                    "triage_thread_id": body.triage_thread_id,
                }
            return "ok", models.release_artifact(
                integration=body.integration,
                type=body.type,
                external_id=body.external_id,
                actor_thread_id=body.actor_thread_id,
                authority=body.authority,
                reason=body.reason,
                source_path=body.source_path,
                triage_thread_id=body.triage_thread_id,
                causal_event_id=body.causal_event_id,
                metadata=body.metadata,
                db=db,
            )

    status, result = await run_in_db(_release)
    if status == "not_found":
        raise HTTPException(404, "Artifact or thread not found")
    if status == "forbidden":
        raise HTTPException(403, result or "Release authority denied")
    assert result is not None
    service.notify_changed(body.model_dump())
    return result


class OwnershipBody(BaseModel):
    """A field left out stays as it is, so one field's picker never touches the other."""

    owner: str | None = None
    supervisor_thread_id: str | None = None


AGENT_CHOICE_REFUSAL = "Pick one of the agents listed."
PERSON_OR_AGENT_CHOICE_REFUSAL = "Pick yourself or one of the agents listed."
NOT_FOUND_REFUSAL = "Couldn’t find this document. Reload the page to see where it is."


def _refusal_for(
    body: OwnershipBody, current: str | None, multi_person: bool, agents: set[str],
) -> tuple[int, str] | None:
    """The status and sentence this change is refused with, or ``None``.

    ``agents`` are the payload's choices, the only agents a pick may name. Only
    a new owner is checked against them; one that stays may be outside them.
    """
    from registry.document_supervisor import MULTI_PERSON_REFUSAL, change_refusal
    from registry.shared_principals import UI_PRINCIPAL

    refusal = change_refusal(current)
    if refusal:
        return 409, refusal
    new_owner = current if body.owner is None else UI_PRINCIPAL if body.owner == "user" else body.owner
    if body.owner not in (None, "user") and new_owner != current and new_owner not in agents:
        return 400, AGENT_CHOICE_REFUSAL if multi_person else PERSON_OR_AGENT_CHOICE_REFUSAL
    if body.supervisor_thread_id and body.supervisor_thread_id not in agents:
        return 400, AGENT_CHOICE_REFUSAL
    if new_owner == UI_PRINCIPAL and current != UI_PRINCIPAL and multi_person:
        return 409, MULTI_PERSON_REFUSAL
    return None


def _apply_change(db, artifact: dict, body: OwnershipBody, current: str | None) -> tuple[dict, list]:
    """Make the change inside the caller's transaction; returns the changed artifact and its events.

    Every comparison is against the stored pick, the one value the header and
    comment delivery read, so the person's explicit choice is always written and
    an event fires only when what is stored changes.
    """
    from registry.document_supervisor import SUPERVISOR_KEY, owner_kind, stored_pick
    from registry.identity_metadata import merge_identity_metadata
    from registry.shared_principals import UI_PRINCIPAL

    artifact_id = artifact["id"]
    stored = stored_pick(artifact)
    new_owner = current if body.owner is None else UI_PRINCIPAL if body.owner == "user" else body.owner
    picked = "supervisor_thread_id" in body.model_fields_set
    supervisor = (body.supervisor_thread_id or None) if picked else stored
    # The agent a person takes a document from supervises it, unless they picked one.
    if new_owner == UI_PRINCIPAL and current != UI_PRINCIPAL and supervisor is None and owner_kind(current) == "agent":
        supervisor = current
    if new_owner != current:
        models.rehome_artifact(
            integration=artifact["integration"], type=artifact["type"], external_id=artifact["external_id"],
            new_owner_thread_id=new_owner, actor_thread_id=None, authority="signed-in-person",
            reason="changed by the signed-in person", source_path="api:/api/artifacts/{id}/ownership", db=db,
        )
    # The owner supervises by default, so picking the owner stores no pick.
    supervisor = supervisor if supervisor != new_owner else None
    events = []
    if new_owner != current:
        events.append(("owner_changed", {"from": owner_kind(current), "to": owner_kind(new_owner)}))
    if supervisor != stored:
        merge_identity_metadata(db, artifact_id, {SUPERVISOR_KEY: supervisor})
        events.append(("supervisor_changed", {"from": "agent" if stored else "none", "to": "agent" if supervisor else "none"}))
    return models.get_artifact(artifact_id, db=db), events


def _change_ownership(artifact_id: str, body: OwnershipBody, user: dict):
    """Returns ``(refusal, None)`` or ``(None, (changed artifact, ownership view, events))``."""
    from infra import config
    from registry.document_supervisor import is_multi_person, ownership
    from registry.shared_principals import artifact_owner

    with write_transaction(None, scope="api_document_ownership") as db:
        artifact = models.get_artifact(artifact_id, db=db)
        # Someone else's document, or an archived or deleted one, reads as missing, the same answer an unknown id gets.
        if (
            artifact is None
            or models.identity_state(artifact_id, db=db) != "active"
            or not may_access_artifact(artifact.get("parent_thread_id"), user["id"], db=db)
        ):
            return (404, NOT_FOUND_REFUSAL), None
        current = artifact_owner(artifact_id, db=db)
        multi_person = is_multi_person(db)
        view = ownership(db, artifact, user_id=user["id"], workspace=config.WORKSPACE, multi_person=multi_person)
        refusal = _refusal_for(body, current, multi_person, {choice["id"] for choice in view["choices"]})
        if refusal:
            return refusal, None
        changed, events = _apply_change(db, artifact, body, current)
        return None, (changed, ownership(db, changed, user_id=user["id"], workspace=config.WORKSPACE, multi_person=multi_person), events)


@router.put("/artifacts/{artifact_id}/ownership")
async def change_document_ownership(artifact_id: str, body: OwnershipBody, user: dict = Depends(require_human_user)):
    """The signed-in person changes a document's owner or supervisor."""
    refusal, result = await run_in_db(lambda: _change_ownership(artifact_id, body, user))
    if refusal:
        raise HTTPException(*refusal)
    assert result is not None
    changed, view, events = result
    service.notify_changed(changed)
    for action, properties in events:
        await asyncio.to_thread(emit_product_event, "documents", action, user_id=user["id"], properties=properties)
    return {"ownership": view}
