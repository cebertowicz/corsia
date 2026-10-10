"""Registry API — the thread artifact pane's lifecycle surface.

Split out from ``api/registry.py`` so that module stays inside its code-health
budget, following the same precedent as ``api/registry_ownership.py``.

- ``GET /api/threads/{id}/artifacts`` — the hierarchical thread pane payload
  (``nodes`` / ``needs_attention`` / ``history``; see ``registry.view``). The
  read itself is side-effect free; safe reconcile repair is scheduled as a
  background task once the response is sent.
- ``GET`` / ``POST /api/threads/{id}/artifacts/reconcile`` — reconcile
  preview (read-only dry-run) and apply (idempotent repair).
- ``POST /api/artifacts/{id}/relocate`` — identity-preserving local file move.

Ownership is checked the same way as the rest of the session routes.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException
from loguru import logger
from pydantic import BaseModel

from doceditor import feedback_batches
from infra.auth import authenticate, may_access_artifact
from infra.database import get_db
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from registry import models, service, view
from registry.doc_freshness import mark_verified
from registry.shared_principals import UI_PRINCIPAL

router = APIRouter(prefix="/api", tags=["registry"])


# Threads with a repair pass already running. Several panes refetching the
# same thread after one broadcast would otherwise each start an identical scan.
# Process-global: this coalesces within one backend process only, and does not
# coordinate across processes. That matches how the pane is served today; a
# multi-process deployment would need a shared lock, not a wider set.
_reconcile_in_flight: set[str] = set()

# Threads whose in-flight pass was triggered again before it finished.
_reconcile_rerun_requested: set[str] = set()


async def _reconcile_after_response(thread_id: str) -> None:
    """Run safe reconcile repair after the pane response has been sent.

    Reading the pane must not mutate, but automatic repair is still
    expected: an applied mutation broadcasts ``artifacts_changed`` and the
    client refetches, so a repaired row disappears shortly after the read.
    Failures are logged, never raised - there is no client left to answer.

    A trigger arriving mid-pass is recorded, never dropped: the running pass
    may already be past its candidate scan, so a file deleted in between
    would otherwise wait for some later unrelated fetch to notice it. A pass
    that starts after the last trigger has necessarily observed everything
    that trigger could want repaired, which is what ends the loop.
    """
    if thread_id in _reconcile_in_flight:
        _reconcile_rerun_requested.add(thread_id)
        return
    _reconcile_in_flight.add(thread_id)
    try:
        while True:
            _reconcile_rerun_requested.discard(thread_id)
            try:
                await run_in_db(
                    lambda: view.reconcile_for_thread(thread_id, mode="apply")
                )
            except Exception as exc:
                logger.warning(
                    f"background artifact reconcile failed for {thread_id}: {exc!r}"
                )
            if thread_id not in _reconcile_rerun_requested:
                return
    finally:
        _reconcile_in_flight.discard(thread_id)
        _reconcile_rerun_requested.discard(thread_id)


@router.get("/threads/{thread_id}/artifacts")
async def get_thread_artifacts(
    thread_id: str, token: str, background: BackgroundTasks,
):
    """Return the artifact pane for a thread the caller owns."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _owns() -> bool:
        return get_thread_for_user(thread_id, user["id"]) is not None

    if not await run_in_db(_owns):
        raise HTTPException(404, "Thread not found")

    def _pane():
        pane = view.pane_for_thread(thread_id)
        db = get_db()
        try:
            return feedback_batches.annotate_pane(pane, user["id"], db=db)
        finally:
            db.close()

    pane = await run_in_db(_pane)
    background.add_task(_reconcile_after_response, thread_id)
    return pane


@router.get("/threads/{thread_id}/artifacts/reconcile")
async def reconcile_thread_artifacts(thread_id: str, token: str):
    """Return a dry-run of known artifact pane repairs for a thread."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _owns() -> bool:
        return get_thread_for_user(thread_id, user["id"]) is not None

    if not await run_in_db(_owns):
        raise HTTPException(404, "Thread not found")

    return await run_in_db(lambda: view.reconcile_for_thread(thread_id, mode="preview"))


@router.post("/threads/{thread_id}/artifacts/reconcile")
async def apply_thread_artifact_reconcile(thread_id: str, token: str):
    """Idempotently apply known artifact pane repairs for a thread."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _owns() -> bool:
        return get_thread_for_user(thread_id, user["id"]) is not None

    if not await run_in_db(_owns):
        raise HTTPException(404, "Thread not found")

    return await run_in_db(lambda: view.reconcile_for_thread(thread_id, mode="apply"))


@router.post("/artifacts/{artifact_id}/verify")
async def verify_artifact_endpoint(artifact_id: str, token: str):
    """Mark one accessible shared document verified by the direct user."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _verify():
        db = get_db()
        try:
            artifact = models.get_artifact(artifact_id, db=db)
            if artifact is None or not may_access_artifact(
                artifact["parent_thread_id"], user["id"], db=db
            ):
                return "not_found", None
            try:
                verified = mark_verified(artifact_id, UI_PRINCIPAL, db=db)
            except PermissionError as exc:
                return "forbidden", str(exc)
            except ValueError as exc:
                return "bad_request", str(exc)
            return "ok", verified
        finally:
            db.close()

    status, result = await run_in_db(_verify)
    if status == "not_found":
        raise HTTPException(404, "Artifact not found")
    if status == "forbidden":
        raise HTTPException(403, result)
    if status == "bad_request":
        raise HTTPException(400, result)
    service.notify_changed(result)
    return {"artifact": result}


class ArtifactRelocateBody(BaseModel):
    thread_id: str
    target_path: str


@router.post("/artifacts/{artifact_id}/relocate")
async def relocate_artifact_endpoint(
    artifact_id: str, token: str, body: ArtifactRelocateBody,
):
    """Move an artifact's backing file, preserving its identity."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _relocate():
        db = get_db()
        try:
            if get_thread_for_user(body.thread_id, user["id"], db=db) is None:
                return "not_found", None
            from registry.relocate import RelocationError, relocate_artifact

            try:
                artifact = relocate_artifact(
                    artifact_id, body.target_path,
                    actor_thread_id=body.thread_id, db=db,
                )
            except RelocationError as exc:
                return "bad_request", str(exc)
            return "ok", artifact
        finally:
            db.close()

    status, result = await run_in_db(_relocate)
    if status == "not_found":
        raise HTTPException(404, "Thread not found")
    if status == "bad_request":
        raise HTTPException(400, result)
    return {"artifact": result}
