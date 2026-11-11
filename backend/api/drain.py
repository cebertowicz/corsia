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
    released = await settle_drain_exit(operation_id, failure_code, reason, backend_healthy)
    if not released:
        raise HTTPException(409, "restart operation changed before cancellation")
    return {"ok": True, "active": False, "operation_id": operation_id}


async def settle_drain_exit(
    operation_id: str, failure_code: str | None = None,
    reason: str | None = None, backend_healthy: bool | None = None,
    *, before_resume: bool = False,
) -> bool:
    """Release the operation using the drain cancellation semantics."""
    from automation.restart import abort_restart_control

    guard = {"before_resume": True} if before_resume else {}
    if failure_code is None:
        released = await abort_restart_control(operation_id, cancelled=True, **guard)
    else:
        if failure_code != "update_refused" or not reason or len(reason) > 500:
            raise HTTPException(400, "invalid update refusal")
        from automation import self_restart
        from infra.db_async import run_in_db
        from infra.update_prepare_output import terminal_update_summary

        reason = terminal_update_summary(reason, None)
        operation = await run_in_db(self_restart.operation_by_id, operation_id)
        if not operation or operation.get("kind") != "update":
            return False
        released = await abort_restart_control(
            operation_id, error=reason, failure_code=failure_code,
            failure_detail=json.dumps({"reason": reason, "backend_healthy": backend_healthy}),
            **guard,
        )
    return released


async def settle_restart_exit(process, operation_id: str, returncode: int) -> None:
    """Settle a child exit from its atomically published lifecycle result."""
    from pathlib import Path
    from automation.restart import abort_restart_control

    if returncode == 0:
        return
    path = None
    try:
        path = Path(process.lifecycle_result_path)
        lines = path.read_text(encoding="utf-8").removesuffix("\n").split("\n")
        fields = dict(line.split("=", 1) for line in lines)
        if (int(fields["status"]) != returncode or
            any(fields[key] not in {"true", "false"} for key in
                ("is_update", "unconfirmed_bootout", "backend_healthy")) or
            len(fields["refusal_reason"]) > 500):
            raise ValueError("invalid lifecycle result")
    except (AttributeError, OSError, UnicodeError, ValueError, KeyError):
        await abort_restart_control(
            operation_id, error=f"restart script exited before restart: {returncode}",
            failure_code="launch_failed", failure_detail=f'{{"exit_code": {returncode}}}',
            before_resume=True,
        )
        return
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    # This backend is reading its child's exit, so teardown did not take it
    # down: releasing is safe unless launchd may still be tearing it down.
    if fields["unconfirmed_bootout"] == "true":
        return
    reason = fields["refusal_reason"] or None
    await settle_drain_exit(
        operation_id, "update_refused" if reason else None, reason,
        fields["backend_healthy"] == "true", before_resume=True,
    )


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
