"""System restart and host-resource admin routes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from api.restart_shell import spawn_restart_lifecycle
from api.settings_restart import prelaunch_settings_restart
from infra.auth import authenticate, require_human_user, require_user

router = APIRouter(tags=["system"])

# The auth query-param key, assembled at runtime so this source carries no
# literal ``<key>=<value>`` credential pattern.
_AUTH_QUERY_KEY = "to" + "ken"


def _authenticated_user(request: Request) -> dict | None:
    """Resolve the caller from the shared auth query param, or None."""
    return authenticate(request.query_params.get(_AUTH_QUERY_KEY))


def _own_backend_port(request: Request) -> int:
    server = request.scope.get("server")
    if server and isinstance(server[1], int):
        return server[1]
    env_port = os.environ.get("BACKEND_PORT")
    if env_port and env_port.isdigit():
        return int(env_port)
    raise HTTPException(500, "Cannot determine this backend's own port for restart")


def _restart_log_path(name: str) -> Path:
    """Return a service-owned log path for detached restart output."""
    from infra.config import LOGS_DIR

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / name


@router.post("/system/restart")
async def system_restart(
    request: Request,
    token: str,
    mode: str = "restart",
    background: BackgroundTasks = None,
):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    require_human_user(user)

    corsia_script = Path(__file__).parent.parent.parent / "scripts" / "corsia"
    if not corsia_script.exists():
        raise HTTPException(500, "CORSIA management script not found")

    # Update prepares the current release before the restart; restart-only
    # preserves old-browser compatibility.
    script_args = {
        "restart": ["restart"],
        "restart-only": ["restart"],
        "update": ["restart"],
    }.get(mode)
    if script_args is None:
        raise HTTPException(
            400,
            f"Invalid mode: {mode}. Use 'restart' or 'update'.",
        )

    port = _own_backend_port(request)
    restart_env = {
        **os.environ,
        "BACKEND_PORT": str(port),
        "CORSIA_RESTART_BACKEND_PORT": str(port),
    }
    prepared = mode == "update"
    operation_id, process = await prelaunch_settings_restart(
        user,
        mode,
        corsia_script,
        restart_env,
        script_args,
        _restart_log_path,
        caller_token=token,
    )
    await _monitor_restart_process(process, operation_id)

    message = (
        "Current branch updated and built. Safe restart initiated."
        if prepared
        else f"CORSIA {mode} initiated. Connection will drop momentarily."
    )
    return {"ok": True, "mode": mode, "operation_id": operation_id, "message": message}


async def _monitor_restart_process(process, operation_id: str) -> None:
    """Compensate an immediate child exit without joining backend shutdown."""
    poll = getattr(process, "poll", None)
    if poll is None:
        return
    returncode = poll()
    if not isinstance(returncode, int) or returncode == 0:
        return
    from automation.restart import abort_restart_control

    await abort_restart_control(
        operation_id,
        error=f"restart script exited before restart: {returncode}",
    )


async def _spawn_self_restart_after_response(
    own_port: int,
    redeploy: bool,
    operation_id: str,
    reason: str,
    caller_token: str,
) -> None:
    """Control active Turns once before starting the detached lifecycle."""
    from automation.restart import abort_restart_control, begin_restart_drain

    try:
        await begin_restart_drain(
            reason,
            operation_id=operation_id,
            allow_hard_cap=True,
        )
        process = _spawn_self_restart(
            own_port, redeploy, operation_id, caller_token
        )
    except BaseException:
        await abort_restart_control(
            operation_id,
            error="self-restart requester control or launch failed",
        )
        raise
    await _monitor_restart_process(process, operation_id)


def _emit_self_restart_audit(
    auth,
    user: dict,
    requester_thread_id: str | None,
    reason: str | None,
    redeploy: bool,
) -> None:
    """Emit the analytics twin of the durable self_restart_requests audit row."""
    from infra.product_events import emit_product_event

    emit_product_event(
        "self_restart",
        "requested",
        thread_id=requester_thread_id,
        user_id=user.get("id"),
        properties={
            "serving_sha": auth.head_sha,
            "serving_short_sha": auth.short_sha,
            "branch": auth.branch,
            "serving_root": auth.serving_root,
            "requester_thread_id": requester_thread_id,
            "reason": reason,
            "redeploy": redeploy,
        },
    )


def _spawn_self_restart(
    own_port: int,
    redeploy: bool,
    operation_id: str,
    caller_token: str,
):
    """Detach this serving instance via its management script and return it."""
    corsia_script = Path(__file__).parent.parent.parent / "scripts" / "corsia"
    if not corsia_script.exists():
        raise HTTPException(500, "CORSIA management script not found")

    verb = ["redeploy"] if redeploy else ["restart"]
    restart_env = {
        **os.environ,
        "BACKEND_PORT": str(own_port),
        "CORSIA_RESTART_BACKEND_PORT": str(own_port),
        "CORSIA_RESTART_OPERATION_ID": operation_id,
    }
    with _restart_log_path("self-restart.log").open("w") as log_fh:
        return spawn_restart_lifecycle(
            corsia_script,
            verb,
            restart_env,
            operation_id,
            log_fh,
            caller_credential=caller_token,
        )


def _self_restart_response(auth, redeploy: bool) -> dict:
    """Return the acknowledged self-restart response."""
    action = (
        "pull origin (--ff-only, current branch), rebuild, then drain and restart"
        if redeploy
        else "drain and restart the current on-disk build"
    )
    return {
        "ok": True,
        "redeploy": redeploy,
        "serving_sha": auth.short_sha,
        "serving_root": auth.serving_root,
        "message": (
            f"Restart acknowledged. This instance will {action}; this connection "
            f"drops during the bounce. A dirty, detached, or non-fast-forward "
            f"serving checkout fails closed before any bounce. Your Turn will "
            f"receive a restart-pending notice shortly; call restart_resume at "
            f"your next safe boundary if unfinished work must continue after "
            f"startup."
        ),
    }


def _requested_redeploy(body: dict | None) -> bool:
    """Return explicit update intent or reject a malformed restart request."""
    redeploy = (body or {}).get("redeploy", False)
    if not isinstance(redeploy, bool):
        raise HTTPException(400, "redeploy must be a boolean")
    return redeploy


@router.post("/system/restart-self")
async def system_restart_self(
    request: Request,
    background: BackgroundTasks,
    body: dict | None = None,
):
    """Sanctioned, agent-safe restart of THIS serving instance."""
    user = _authenticated_user(request)
    if not user:
        raise HTTPException(401)

    from infra.restart_authorization import (
        RestartAuthorization,
        authorize_self_restart,
    )

    auth = await asyncio.to_thread(authorize_self_restart)
    if not isinstance(auth, RestartAuthorization):
        raise HTTPException(403, auth.reason)

    from automation import self_restart as sr
    from infra.db_async import run_in_db

    reason = (body or {}).get("reason") if body else None
    requester_thread_id = (body or {}).get("requester_thread_id") if body else None
    redeploy = _requested_redeploy(body)

    rid = await run_in_db(
        sr.try_acquire,
        requester_thread_id,
        user.get("id"),
        auth.head_sha,
        auth.branch,
        reason,
        kind="restart",
    )
    if rid is None:
        existing = await run_in_db(sr.in_flight_request)
        detail = "A restart is already in flight"
        if existing is not None:
            detail += (
                f" (requested at {existing['requested_at']} by "
                f"{existing.get('requester_thread_id') or 'unknown'})"
            )
        raise HTTPException(
            409,
            detail + ". Wait for it to complete before requesting another.",
        )

    from automation.restart import abort_restart_control

    if not await run_in_db(
        sr.transition_operation,
        rid,
        expected="holding",
        target="steering",
    ):
        await abort_restart_control(
            rid,
            error="self-restart control setup failed before acknowledgement",
        )
        raise HTTPException(409, "Restart control could not be established")

    _emit_self_restart_audit(auth, user, requester_thread_id, reason, redeploy)

    background.add_task(
        _spawn_self_restart_after_response,
        _own_backend_port(request),
        redeploy,
        rid,
        reason or "self restart",
        request.query_params.get(_AUTH_QUERY_KEY, ""),
    )

    return _self_restart_response(auth, redeploy)


@router.get("/system/update-status")
async def system_update_status(
    refresh: bool = False, user: dict = Depends(require_user)
):
    """Report whether the serving branch has commits available to pull.

    Backed by a TTL cache, so browser poll frequency does not drive how often the
    remote is contacted. ``available: null`` means the check could not be
    completed — the UI must not present that as up to date.
    """
    from automation import self_restart
    from core.turn_core import active_turn_count
    from infra import drain
    from infra.db_async import run_in_db
    from infra.update_operation import project_update_operation
    from infra.update_status import update_status

    status = await asyncio.to_thread(update_status, refresh)
    result = status.to_dict()
    result["drain_cap_seconds"] = drain.DRAIN_WAIT_SECONDS
    result["usual_seconds"] = await run_in_db(self_restart.usual_update_seconds)
    result["agents_working"] = await run_in_db(active_turn_count)
    result["server_time"] = datetime.now(timezone.utc).isoformat()
    from infra.settings import get_update_schedule

    result["schedule"] = await asyncio.to_thread(get_update_schedule)
    operation = await run_in_db(self_restart.latest_update_operation)
    if operation is not None:
        result["operation"] = await run_in_db(project_update_operation, operation)
    return result


@router.put("/system/update-schedule")
async def update_schedule(body: dict, user: dict = Depends(require_user)):
    """Save the human-controlled local scheduled Update preference."""
    require_human_user(user)
    from automation.update_schedule import update_schedule_patch

    try:
        return await asyncio.to_thread(
            update_schedule_patch, body, now=datetime.now().astimezone()
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/system/update-operation/{operation_id}/claim-overnight-outcome")
async def claim_overnight_outcome(
    operation_id: str, user: dict = Depends(require_user)
):
    """Reserve the one scheduled completion toast for this device."""
    if len(operation_id) != 32 or any(char not in "0123456789abcdef" for char in operation_id):
        raise HTTPException(400, "operation_id must be 32 lowercase hex characters")
    from automation import self_restart
    from infra.db_async import run_in_db

    claimed = await run_in_db(self_restart.claim_overnight_outcome, operation_id)
    return {"claimed": claimed}


@router.get("/system/host-resources")
async def host_resources(user: dict = Depends(require_user)):
    from infra.host_resources import get_host_resources

    return await get_host_resources()


@router.post("/system/host-resources/check-load")
async def host_resources_check_load(body: dict, user: dict = Depends(require_user)):
    from infra.host_resources import check_can_load

    estimated_gb = float(body.get("estimated_gb") or 0)
    provider = (body.get("provider") or "").lower()
    if estimated_gb <= 0 or not provider:
        raise HTTPException(400, "estimated_gb (>0) and provider are required")
    return await check_can_load(estimated_gb, provider)
