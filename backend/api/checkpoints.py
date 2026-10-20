"""Authenticated reads and exact mutations for retained checkpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from core.checkpoint_agent_admission import CheckpointNotReady, prepare_external_checkpoint
from core.checkpoint_types import CheckpointRecord, CheckpointSummary
from infra.auth import authenticate
from infra.db_async import run_in_db


router = APIRouter()


async def _prepare_manual(thread_id: str, user_id: str):
    """Admit an unthresholded manual checkpoint, cold when no warm source exists."""
    return await prepare_external_checkpoint(thread_id, user_id, trigger="manual")


def _project(record: CheckpointRecord | CheckpointSummary) -> dict[str, object | None]:
    return {
        "id": record.id,
        "trigger": record.trigger,
        "mode": record.mode,
        "status": record.status,
        "writerModel": record.writer_model,
        "error": record.error,
        "startedAt": record.started_at,
    }


async def _owned_latest(thread_id: str, user_id: str):
    from infra.checkpoint_queries import latest_for_thread
    from infra.database import get_db
    from infra.queries import get_thread_for_user

    def read():
        db = get_db()
        try:
            if get_thread_for_user(thread_id, user_id, db=db) is None:
                return None
            return True, latest_for_thread(thread_id, db=db)
        finally:
            db.close()

    return await run_in_db(read)


async def _owned_active(thread_id: str, user_id: str):
    from infra.checkpoint_queries import active_for_thread
    from infra.database import get_db
    from infra.queries import get_thread_for_user

    def read():
        db = get_db()
        try:
            if get_thread_for_user(thread_id, user_id, db=db) is None:
                return None
            return True, active_for_thread(thread_id, db=db)
        finally:
            db.close()

    return await run_in_db(read)


async def _owned_checkpoint(thread_id: str, checkpoint_id: str, user_id: str):
    from infra.checkpoint_queries import read
    from infra.database import get_db
    from infra.queries import get_thread_for_user

    def read_owned():
        db = get_db()
        try:
            if get_thread_for_user(thread_id, user_id, db=db) is None:
                return None
            record = read(checkpoint_id, db=db)
            return record if record is not None and record.thread_id == thread_id else None
        finally:
            db.close()

    return await run_in_db(read_owned)


@router.get("/threads/{thread_id}/checkpoints")
async def get_checkpoints(thread_id: str, token: str = ""):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    owned = await _owned_latest(thread_id, user["id"])
    if owned is None:
        raise HTTPException(404)
    _exists, record = owned
    return {"checkpoints": [] if record is None else [_project(record)]}


@router.post("/threads/{thread_id}/checkpoints")
async def request_manual_checkpoint(thread_id: str, token: str = ""):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    owned = await _owned_active(thread_id, user["id"])
    if owned is None:
        raise HTTPException(404)
    _exists, active = owned
    if active is not None:
        return JSONResponse(status_code=202, content={"checkpoint": _project(active)})
    try:
        prepared = await _prepare_manual(thread_id, user["id"])
    except CheckpointNotReady as error:
        raise HTTPException(
            409, {"message": str(error), "reason": error.reason_key, **error.details}
        ) from error
    if prepared is None:
        raise HTTPException(404)
    from core.checkpoint_runtime import CheckpointTargetLimited, request_checkpoint

    try:
        record = await request_checkpoint(*prepared)
    except CheckpointTargetLimited as error:
        raise HTTPException(
            409, {"message": str(error), "reason": "target_limited"}
        ) from error
    return JSONResponse(status_code=202, content={"checkpoint": _project(record)})


@router.post("/threads/{thread_id}/checkpoints/{checkpoint_id}/cancel")
async def cancel_exact_checkpoint(
    thread_id: str,
    checkpoint_id: str,
    token: str = "",
):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    if await _owned_checkpoint(thread_id, checkpoint_id, user["id"]) is None:
        raise HTTPException(404)
    from core.checkpoint_runtime import cancel_checkpoint

    return {"checkpoint": _project(await cancel_checkpoint(checkpoint_id))}
