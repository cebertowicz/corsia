"""A2.5 explicit artifact ownership transfer/release endpoints.

Split out from ``api/registry.py`` so the generic register/resolve/pane surface
stays inside its code-health budget. The generic ``POST /register`` is
owner-preserving; these endpoints are the audited rescue/handoff/release path.
Authority is checked against the SOURCE scope (current owner when present, else
explicit triage, else the latest release event's old owner) — passive
subscribers never define transfer authority.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from infra.auth import authenticate, require_human_user
from infra.database import write_transaction
from infra.db_async import run_in_db
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
    owner: str
    supervisor_thread_id: str | None = None


MULTI_PERSON_REFUSAL = "Taking over a document isn't available yet when more than one person uses this CORSIA."


@router.put("/artifacts/{artifact_id}/ownership")
async def change_document_ownership(artifact_id: str, body: OwnershipBody, user: dict = Depends(require_human_user)):
    """The signed-in person changes a document's owner or supervisor (O1 prototype)."""
    from infra import config
    from registry.document_supervisor import SUPERVISOR_KEY, TAKEN_OVER_KEY, ownership, person_accounts
    from registry.identity_metadata import merge_identity_metadata, parse_metadata
    from registry.shared_principals import UI_PRINCIPAL, artifact_owner

    def _change():
        with write_transaction(None, scope="api_document_ownership") as db:
            artifact = models.get_artifact(artifact_id, db=db)
            if artifact is None or artifact.get("state") != "active":
                return "not_found", None
            current = artifact_owner(artifact_id, db=db)
            view = ownership(db, artifact, user_id=user["id"], workspace=config.WORKSPACE)
            agents = {choice["id"] for choice in view["choices"]}
            new_owner = UI_PRINCIPAL if body.owner == "user" else body.owner
            if new_owner != UI_PRINCIPAL and new_owner not in agents:
                return "bad_choice", None
            if body.supervisor_thread_id and body.supervisor_thread_id not in agents:
                return "bad_choice", None
            if UI_PRINCIPAL in {current, new_owner} and person_accounts(db) > 1:
                return "multi_person", None
            supervisor = body.supervisor_thread_id
            if new_owner == UI_PRINCIPAL and supervisor is None and current and current != UI_PRINCIPAL:
                supervisor = current
            if new_owner != current:
                models.rehome_artifact(
                    integration=artifact["integration"], type=artifact["type"], external_id=artifact["external_id"],
                    new_owner_thread_id=new_owner, actor_thread_id=None, authority="signed-in-person",
                    reason="changed by the signed-in person", source_path="api:/api/artifacts/{id}/ownership", db=db,
                )
            metadata = parse_metadata(artifact.get("metadata"))
            updates = {SUPERVISOR_KEY: supervisor if new_owner == UI_PRINCIPAL else (supervisor if supervisor != new_owner else None)}
            updates[TAKEN_OVER_KEY] = user["id"] if new_owner == UI_PRINCIPAL else None
            if updates != {key: metadata.get(key) for key in updates}:
                merge_identity_metadata(db, artifact_id, updates)
            return "ok", ownership(db, models.get_artifact(artifact_id, db=db), user_id=user["id"], workspace=config.WORKSPACE)

    status, result = await run_in_db(_change)
    if status == "not_found":
        raise HTTPException(404, "Document not found")
    if status == "bad_choice":
        raise HTTPException(400, "Pick yourself or an agent in this document's scope.")
    if status == "multi_person":
        raise HTTPException(409, MULTI_PERSON_REFUSAL)
    return {"ownership": result}
