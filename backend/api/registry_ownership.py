"""A2.5 explicit artifact ownership transfer/release endpoints.

Split out from ``api/registry.py`` so the generic register/resolve/pane surface
stays inside its code-health budget. The generic ``POST /register`` is
owner-preserving; these endpoints are the audited rescue/handoff/release path.
Authority is checked against the SOURCE scope (current owner when present, else
explicit triage, else the latest release event's old owner) — passive
subscribers never define transfer authority.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from infra.auth import authenticate
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
