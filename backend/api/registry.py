"""Registry API — the artifacts primitive's HTTP surface.

- ``GET /api/projects/{id}/artifacts`` — project-scope listing (the documents
  lens): artifacts owned by the project's threads plus the project-sentinel
  rows (``parent_thread_id = 'project:<id>'``, e.g. bootload designations).
- ``POST /api/artifacts/register`` / ``GET /api/artifacts/resolve`` — the ONE
  exposed register/resolve surface for out-of-process callers (the CC MCP,
  doc-open). Thin 1:1 mirrors over ``registry.models``; path-bearing
  registrations go through the same registrable-root policy as the observer
  (``registry.service``) — an explicit API call gets an explicit 400, not a
  silent skip.

The thread pane, reconcile and relocate routes live in
``api/registry_pane.py``; explicit ownership transfer lives in
``api/registry_ownership.py``.

Ownership is checked the same way as the rest of the session routes.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from infra.auth import (
    authenticate,
    is_owner_sentinel,
    may_access_artifact,
    owns_thread,
)
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.queries import get_project, get_thread_for_user
from registry import models, pinning, service
from registry.file_resolution import normalize_file_path, resolve_file_identity
from registry.removal_events import (
    broadcast_artifact_removal_event,
    emit_artifact_removal_event,
)

router = APIRouter(prefix="/api", tags=["registry"])


@router.get("/projects/{project_id}/artifacts")
async def get_project_artifacts(
    project_id: str,
    token: str,
    type: str | None = None,
    state: str = "active",
    include_ephemeral: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Project-scope artifact listing, most-recently-touched first.

    Scope = rows whose owning thread belongs to the project, plus the
    project's sentinel-owned rows (bootload designations). Defaults to the
    active, durable-retention view; ``type=document`` is the documents lens.
    """
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _query():
        db = get_db()
        try:
            if get_project(project_id, db=db) is None:
                return None
            return models.list_for_project(
                project_id, type=type, state=state,
                include_ephemeral=include_ephemeral,
                limit=limit, offset=offset, db=db,
            )
        finally:
            db.close()

    result = await run_in_db(_query)
    if result is None:
        raise HTTPException(404, "Project not found")
    return result


class RegisterBody(BaseModel):
    integration: str
    type: str
    external_id: str
    parent_thread_id: str
    title: str | None = None
    description: str | None = None
    path: str | None = None
    retention: str | None = None
    metadata: dict | None = None


async def _write_registered_artifact(
    body: RegisterBody,
    *,
    metadata: dict | None,
    path: str | None,
    retention_default: bool,
    canonical_workspace_file: bool,
) -> dict:
    """Persist a validated API registration through its canonical writer."""
    if canonical_workspace_file:
        return await run_in_db(
            lambda: service.register_write_strict(
                body.parent_thread_id,
                path,
                body.description,
                title=body.title,
                retention=body.retention,
                reject_retention_change=True,
                metadata=metadata,
            )
        )
    return await run_in_db(lambda: models.register_artifact(
        integration=body.integration,
        type=body.type,
        external_id=body.external_id,
        parent_thread_id=body.parent_thread_id,
        title=body.title,
        description=body.description,
        path=path,
        retention=body.retention,
        retention_default=retention_default,
        reject_retention_change=True,
        metadata=metadata,
    ))


@router.post("/artifacts/register")
async def register_artifact(token: str, body: RegisterBody):
    """Register (upsert) an artifact to a thread the caller owns.

    Same identity → the existing row re-activates and the owner is preserved
    (registering something already owned does not re-home it). A path outside
    the registrable roots is a loud 400.
    """
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    if not (body.integration.strip() and body.type.strip() and body.external_id.strip()):
        raise HTTPException(400, "integration, type and external_id are required")
    if body.retention is not None and body.retention not in ("ephemeral", "durable"):
        raise HTTPException(400, "retention must be 'ephemeral' or 'durable'")

    def _owns() -> bool:
        return get_thread_for_user(body.parent_thread_id, user["id"]) is not None

    if not await run_in_db(_owns):
        raise HTTPException(404, "Thread not found")

    metadata, path = body.metadata, body.path
    retention_default = False
    canonical_workspace_file = False
    if path is not None and "://" not in path:
        # A local file path (URLs — PR links, upload refs — pass through
        # untouched): same normalization as the observer path, in run_in_db
        # (resolve + the root policy walk the filesystem).
        normalized = await run_in_db(lambda: normalize_file_path(path))
        if normalized is None:
            raise HTTPException(
                400, f"path is outside the registrable roots: {path}")
        path, zone, derived_ephemeral_default = normalized
        metadata = {**(metadata or {}), "zone": zone}
        retention_default = derived_ephemeral_default  # working/-tray default for a fresh row
        canonical_workspace_file = body.integration == "workspace"

    try:
        row = await _write_registered_artifact(
            body,
            metadata=metadata,
            path=path,
            retention_default=retention_default,
            canonical_workspace_file=canonical_workspace_file,
        )
    except models.RetentionChangeDenied as exc:
        raise HTTPException(400, str(exc)) from exc
    service.notify_changed(row or {
        "integration": body.integration,
        "type": body.type,
        "external_id": body.external_id,
    })
    # Register never re-homes (A2.5): report the surviving owner + conflict so a
    # rescue caller learns it is a passive subscriber, not the new owner.
    current_owner = row["parent_thread_id"] if row else None
    conflict = current_owner is not None and current_owner != body.parent_thread_id
    relationship_state = (
        "subscriber-recent" if row is None or conflict else "owner-active"
    )
    return {
        "ok": True,
        "artifact": row,
        "ownership": {
            "ownership_changed": False,
            "conflict": conflict,
            "requested_owner_thread_id": body.parent_thread_id,
            "current_owner_thread_id": current_owner,
            "relationship_state": relationship_state,
        },
    }


@router.get("/artifacts/resolve")
async def resolve_artifact(
    token: str,
    integration: str | None = None,
    type: str | None = None,
    external_id: str | None = None,
    path: str | None = None,
):
    """Resolve an exact identity or a registered file's physical path."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    identity_fields = (integration, type, external_id)
    if path is not None:
        if any(value is not None for value in identity_fields):
            raise HTTPException(400, "provide path or an exact identity, not both")
        if not path.strip():
            raise HTTPException(400, "path is required")
        row = await run_in_db(lambda: resolve_file_identity(path))
    else:
        if any(value is None for value in identity_fields):
            raise HTTPException(
                400,
                "integration, type and external_id are required when path is absent",
            )
        row = await run_in_db(
            lambda: models.resolve(integration, type, external_id)
        )
    return {"found": row is not None, "artifact": row}


class ArtifactPatch(BaseModel):
    description: str


class ArtifactDisplayPatch(BaseModel):
    thread_id: str
    hidden: bool


class ArtifactPinnedPatch(BaseModel):
    thread_id: str
    pinned: bool


@router.patch("/artifacts/{artifact_id}")
async def patch_artifact(artifact_id: str, token: str, body: ArtifactPatch):
    """Edit an artifact's description (Documents surface; doc-editor slice).

    The caller must own the artifact's owning thread, exactly as
    ``POST /register`` requires. A 404 (not 403) on a foreign or absent
    artifact keeps cross-user ids non-enumerable.
    """
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _patch() -> dict | None:
        db = get_db()
        try:
            art = models.get_artifact(artifact_id, db=db)
            if art is None or not may_access_artifact(art["parent_thread_id"], user["id"], db=db):
                return None
            return models.update_description(artifact_id, body.description, db=db)
        finally:
            db.close()

    row = await run_in_db(_patch)
    if row is None:
        raise HTTPException(404, "Artifact not found")
    service.notify_changed(row)
    return {"artifact": row}


@router.patch("/artifacts/{artifact_id}/display")
async def patch_artifact_display(
    artifact_id: str,
    token: str,
    body: ArtifactDisplayPatch,
):
    """Hide or unhide an artifact from its owning thread pane view."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    if not body.thread_id.strip():
        raise HTTPException(400, "thread_id is required")

    def _patch() -> tuple[dict, bool, dict | None] | None:
        db = get_db()
        try:
            if get_thread_for_user(body.thread_id, user["id"], db=db) is None:
                return None
            owner = models.get_artifact(artifact_id, db=db)
            if owner is None or is_owner_sentinel(owner["parent_thread_id"]):
                return None
            target_surfacing = models.SURFACING_QUIET if body.hidden else models.SURFACING_VISIBLE
            changed = owner.get("surfacing") != target_surfacing
            with write_transaction(db, scope="patch_artifact_display") as connection:
                row = models.update_surfacing(
                    artifact_id,
                    surfacing=target_surfacing,
                    parent_thread_id=body.thread_id,
                    db=connection,
                )
                if row is None:
                    return None
                event = None
                if changed:
                    event = emit_artifact_removal_event(
                        thread_id=body.thread_id,
                        artifact_id=artifact_id,
                        artifact_type=row["type"],
                        title=row.get("title"),
                        subtype="hidden_from_pane" if body.hidden else "shown_in_pane",
                        actor_kind="user",
                        hidden=body.hidden,
                        db=connection,
                    )
                return row, changed, event
        finally:
            db.close()

    result = await run_in_db(_patch)
    if result is None:
        raise HTTPException(404, "Artifact not found")
    row, changed, event = result
    if changed and event is not None:
        broadcast_artifact_removal_event(event)
    service.notify_changed(row)
    return {"artifact": row}


@router.patch("/artifacts/{artifact_id}/pinned")
async def patch_artifact_pinned(
    artifact_id: str,
    token: str,
    body: ArtifactPinnedPatch,
):
    """Set whether an owner registration persists past task settlement."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    if not body.thread_id.strip():
        raise HTTPException(400, "thread_id is required")

    def _patch() -> dict | None:
        db = get_db()
        try:
            if get_thread_for_user(body.thread_id, user["id"], db=db) is None:
                return None
            owner = models.get_artifact(artifact_id, db=db)
            if (
                owner is None
                or is_owner_sentinel(owner["parent_thread_id"])
                or not may_access_artifact(owner["parent_thread_id"], user["id"], db=db)
            ):
                return None
            return pinning.update_pinned(
                artifact_id,
                pinned=body.pinned,
                parent_thread_id=body.thread_id,
                db=db,
            )
        finally:
            db.close()

    row = await run_in_db(_patch)
    if row is None:
        raise HTTPException(404, "Artifact not found")
    service.notify_changed(row)
    return {"artifact": row}
