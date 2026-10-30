"""Drain mode routes — durable restart coordination."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, BackgroundTasks, HTTPException

from infra.auth import authenticate

router = APIRouter(prefix="/api/system", tags=["drain"])


def _require_user(token: str) -> dict:
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    return user


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
    """Return durable operation truth plus process-local readiness observations."""
    user = _require_user(token)
    from infra.drain import get_drain_operation_status, get_drain_status

    if operation_id:
        status = get_drain_operation_status(operation_id)
        if status is None:
            raise HTTPException(404, "restart operation not found")
        return status
    return get_drain_status(user_id=user["id"])


@router.delete("/drain")
async def cancel_drain(
    token: str, operation_id: str, failure_code: str | None = None,
    reason: str | None = None, backend_healthy: bool | None = None,
):
    """Release only the exact active operation, recording a pre-stop refusal."""
    _require_user(token)
    from automation.restart import abort_restart_control

    if failure_code is None:
        released = await abort_restart_control(operation_id, cancelled=True)
    else:
        if failure_code != "update_refused" or not reason or len(reason) > 500:
            raise HTTPException(400, "invalid update refusal")
        from automation import self_restart
        from infra.db_async import run_in_db

        operation = await run_in_db(self_restart.operation_by_id, operation_id)
        if not operation or operation.get("kind") != "update":
            raise HTTPException(409, "update operation changed before refusal")
        released = await abort_restart_control(
            operation_id, error=reason, failure_code=failure_code,
            failure_detail=json.dumps({"reason": reason, "backend_healthy": backend_healthy}),
        )
    if not released:
        raise HTTPException(409, "restart operation changed before cancellation")
    return {"ok": True, "active": False, "operation_id": operation_id}


@router.post("/drain/update")
async def confirm_update(token: str, body: dict):
    """Receive the lifecycle shell's health observation in the running backend."""
    user = _require_user(token)
    from automation import self_restart
    from automation.update_verification import confirm_packaged_update
    from infra.db_async import run_in_db

    operation_id = body.get("operation_id")
    if not isinstance(operation_id, str) or not isinstance(body.get("healthy"), bool):
        raise HTTPException(400, "operation_id and healthy are required")
    operation = await run_in_db(self_restart.operation_by_id, operation_id)
    if operation is None:
        raise HTTPException(404, "Update operation not found")
    if operation.get("requester_user") not in {None, user["id"]}:
        raise HTTPException(403, "Update belongs to another user")
    code = body.get("failure_code", "version_unverified")
    if code not in {"version_unverified", "wrong_version_running"}:
        raise HTTPException(400, "invalid Update failure code")
    settled = await confirm_packaged_update(
        operation_id, healthy=body["healthy"], reason=str(body.get("reason") or ""),
        failure_code=code,
    )
    if settled is None:
        raise HTTPException(409, "Update operation changed before confirmation")
    return {"ok": True, "operation_id": operation_id, "status": settled["status"],
            "failure_code": settled.get("failure_code")}


@router.post("/drain/ready")
async def drain_ready(token: str, body: dict):
    """Advance one quiescent exact operation to process-restart ownership."""
    _require_user(token)
    from automation.restart import advance_restart_readiness, mark_restarting

    operation_id = str(body.get("operation_id") or "")
    if not operation_id:
        raise HTTPException(400, "operation_id is required")
    if not advance_restart_readiness(operation_id):
        raise HTTPException(409, "restart operation is not quiescent")
    if not mark_restarting(operation_id):
        raise HTTPException(409, "restart operation could not enter restarting")
    return {"ok": True, "operation_id": operation_id, "phase": "restarting"}


@router.post("/drain/hard-cap")
async def drain_hard_cap(token: str, body: dict):
    """Persist recovery and terminalize exact remaining Turns before force-stop."""
    _require_user(token)
    from automation.restart import enforce_restart_hard_cap, mark_restarting

    operation_id = str(body.get("operation_id") or "")
    if not operation_id:
        raise HTTPException(400, "operation_id is required")
    try:
        outcome = await enforce_restart_hard_cap(operation_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if outcome["force_stop_ready"] and not mark_restarting(operation_id):
        raise HTTPException(409, "restart operation could not enter restarting")
    return {"ok": outcome["force_stop_ready"], "operation_id": operation_id, **outcome}
