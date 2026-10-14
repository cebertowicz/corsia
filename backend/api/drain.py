"""Drain mode routes — durable restart coordination."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, HTTPException

from infra.auth import authenticate

router = APIRouter(prefix="/api/system", tags=["drain"])


def _require_user(token: str) -> dict:
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    return user


async def _require_lifecycle_callback(token: str, operation_id: str) -> None:
    """Authorize a normal caller or this scheduled operation's shell only."""
    if authenticate(token):
        return
    from automation import self_restart
    from infra.db_async import run_in_db

    if await run_in_db(self_restart.callback_credential_matches, operation_id, token):
        return
    raise HTTPException(401)


@router.post("/drain")
async def system_drain(
    background: BackgroundTasks,
    token: str,
    body: dict | None = None,
):
    """Acknowledge the exact durable cutoff before bounded restart control."""
    user = _require_user(token)
    from automation.restart import begin_restart_drain

    payload = body or {}
    reason = payload.get("reason") or "manual"
    requested_id = payload.get("operation_id")
    if requested_id is None:
        raise HTTPException(400, "operation_id is required")
    if (
        not isinstance(requested_id, str)
        or len(requested_id) != 32
        or any(char not in "0123456789abcdef" for char in requested_id)
    ):
        raise HTTPException(400, "operation_id must be 32 lowercase hex characters")
    from automation import self_restart
    from infra.db_async import run_in_db

    operation_id = await run_in_db(
        self_restart.try_acquire_steering,
        None,
        None,
        None,
        None,
        str(reason),
        operation_id=requested_id,
    )
    if operation_id is None:
        raise HTTPException(409, "could not acquire restart cutoff")

    background.add_task(
        begin_restart_drain,
        str(reason),
        operation_id=operation_id,
        allow_hard_cap=True,
    )
    from infra.drain import get_drain_status

    status = await asyncio.to_thread(get_drain_status, user_id=user["id"])
    return {"ok": True, **status}


@router.get("/drain")
async def get_drain_status_route(token: str, operation_id: str | None = None):
    """Return the caller's status, or an exact callback-owned operation's status."""
    from infra.drain import get_drain_operation_status, get_drain_status

    if operation_id:
        await _require_lifecycle_callback(token, operation_id)
        status = get_drain_operation_status(operation_id)
        if status is None:
            raise HTTPException(404, "restart operation not found")
        return status
    user = _require_user(token)
    return get_drain_status(user_id=user["id"])


@router.delete("/drain")
async def cancel_drain(token: str, operation_id: str):
    """Compensate and cancel only the exact active restart operation."""
    await _require_lifecycle_callback(token, operation_id)
    from automation.restart import abort_restart_control

    if not await abort_restart_control(operation_id, cancelled=True):
        raise HTTPException(409, "restart operation changed before cancellation")
    return {"ok": True, "active": False, "operation_id": operation_id}


@router.post("/drain/ready")
async def drain_ready(token: str, body: dict):
    """Advance one quiescent exact operation to process-restart ownership."""
    operation_id = str(body.get("operation_id") or "")
    if not operation_id:
        raise HTTPException(400, "operation_id is required")
    await _require_lifecycle_callback(token, operation_id)
    from automation.restart import advance_restart_readiness, mark_restarting
    if not advance_restart_readiness(operation_id):
        raise HTTPException(409, "restart operation is not quiescent")
    if not mark_restarting(operation_id):
        raise HTTPException(409, "restart operation could not enter restarting")
    return {"ok": True, "operation_id": operation_id, "phase": "restarting"}


@router.post("/drain/hard-cap")
async def drain_hard_cap(token: str, body: dict):
    """Persist recovery and terminalize exact remaining Turns before force-stop."""
    operation_id = str(body.get("operation_id") or "")
    if not operation_id:
        raise HTTPException(400, "operation_id is required")
    await _require_lifecycle_callback(token, operation_id)
    from automation.restart import enforce_restart_hard_cap, mark_restarting
    try:
        outcome = await enforce_restart_hard_cap(operation_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if outcome["force_stop_ready"] and not mark_restarting(operation_id):
        raise HTTPException(409, "restart operation could not enter restarting")
    return {"ok": outcome["force_stop_ready"], "operation_id": operation_id, **outcome}
