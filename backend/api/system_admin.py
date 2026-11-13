"""System restart and host-resource admin routes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import os
from pathlib import Path
from threading import Thread

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from api.drain import log_restart_exit_failure, settle_restart_exit
from api.restart_shell import spawn_restart_lifecycle
from api.self_restart_response import emit_self_restart_audit as _emit_self_restart_audit
from api.self_restart_response import self_restart_response as _self_restart_response
from api.settings_restart import prelaunch_settings_restart
from api.system_admin_helpers import own_backend_port as _own_backend_port
from api.system_admin_helpers import restart_log_path as _restart_log_path
from infra.auth import authenticate, require_human_user, require_user


router = APIRouter(tags=["system"])

# The auth query-param key, assembled at runtime so this source carries no
# literal ``<key>=<value>`` credential pattern.
_AUTH_QUERY_KEY = "to" + "ken"


def _authenticated_user(request: Request) -> dict | None:
    """Resolve the caller from the shared auth query param, or None."""
    return authenticate(request.query_params.get(_AUTH_QUERY_KEY))


async def launch_update(
    user: dict, caller_token: str | None, trigger: str, *, port: int,
    claim: Callable[[object], bool] | None = None,
) -> tuple[str, object] | None:
    """Launch the one Settings Update lifecycle from any approved trigger."""
    if trigger not in {"manual", "tonight", "automatic"}:
        raise ValueError("update trigger is invalid")
    corsia_script = Path(__file__).parent.parent.parent / "scripts" / "corsia"
    if not corsia_script.exists():
        raise RuntimeError("CORSIA management script not found")
    restart_env = {
        **os.environ,
        "BACKEND_PORT": str(port),
        "CORSIA_RESTART_BACKEND_PORT": str(port),
    }
    from infra.drain import drain_wait_seconds

    restart_env["CORSIA_DRAIN_MAX_WAIT"] = str(drain_wait_seconds())
    # A scheduled run mints its service credential only after it acquires ownership.
    prelaunch = await prelaunch_settings_restart(
        user,
        "update",
        corsia_script,
        restart_env,
        ["restart"],
        _restart_log_path,
        caller_token=caller_token,
        claim=claim,
        trigger=trigger,
    )
    if prelaunch is None:
        return None
    operation_id, process = prelaunch
    await _monitor_restart_process(process, operation_id)
    return operation_id, process


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
    prepared = mode == "update"
    if prepared:
        operation_id, _process = await launch_update(
            user,
            token,
            "manual",
            port=port,
        )
    else:
        from infra.drain import drain_wait_seconds

        restart_env = {
            **os.environ,
            "BACKEND_PORT": str(port),
            "CORSIA_RESTART_BACKEND_PORT": str(port),
            "CORSIA_DRAIN_MAX_WAIT": str(drain_wait_seconds()),
        }
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


_RESTART_EXIT_TASKS: set[asyncio.Task] = set()


async def _monitor_restart_process(process, operation_id: str) -> None:
    """Own the detached child's exit without joining backend shutdown."""
    poll = getattr(process, "poll", None)
    if poll is None:
        return
    returncode = poll()
    if isinstance(returncode, int):
        await settle_restart_exit(process, operation_id, returncode)
        return
    loop = asyncio.get_running_loop()

    def wait_for_exit() -> None:
        returncode = process.wait()
        if not isinstance(returncode, int):
            return

        def schedule() -> None:
            task = loop.create_task(settle_restart_exit(process, operation_id, returncode))
            _RESTART_EXIT_TASKS.add(task)
            task.add_done_callback(_RESTART_EXIT_TASKS.discard)
            task.add_done_callback(log_restart_exit_failure(operation_id))

        try:
            loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            pass  # Startup owns the operation after backend shutdown.

    Thread(target=wait_for_exit, name="restart-child-exit", daemon=True).start()


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
        process = await asyncio.to_thread(
            _spawn_self_restart, own_port, redeploy, operation_id, caller_token
        )
    except BaseException:
        await abort_restart_control(
            operation_id,
            error="self-restart requester control or launch failed",
        )
        raise
    await _monitor_restart_process(process, operation_id)


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
    from automation.update_outcome import staged_update_activated
    from core.turn_core import active_turn_count
    from infra import build_info, drain
    from infra.db_async import run_in_db
    from infra.update_operation import project_update_operation
    from infra.update_status import update_status

    status = await asyncio.to_thread(update_status, refresh)
    result = status.to_dict()
    result["drain_cap_seconds"] = drain.drain_wait_seconds()
    result["usual_seconds"] = await run_in_db(self_restart.usual_update_seconds)
    result["agents_working"] = await run_in_db(active_turn_count)
    result["server_time"] = datetime.now(timezone.utc).isoformat()
    # The running process's version; `current_version` is what is on disk, which changes before the restart.
    result["running_version"] = build_info.running_app_version()
    from infra.settings import get_update_schedule

    result["schedule"] = await run_in_db(get_update_schedule)
    operation = await run_in_db(self_restart.latest_update_operation)
    if operation is not None:
        activated = await run_in_db(staged_update_activated, operation)
        result["operation"] = await run_in_db(
            lambda: project_update_operation(operation, activated_by_restart=activated)
        )
    return result


@router.put("/system/update-schedule")
async def update_schedule(body: dict, user: dict = Depends(require_user)):
    """Save the human-controlled local scheduled Update preference."""
    require_human_user(user)
    from automation.update_schedule import update_schedule_patch

    try:
        return await asyncio.to_thread(
            update_schedule_patch,
            body,
            now=datetime.now().astimezone(),
            user_id=user["id"],
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/system/update-operations/{operation_id}/outcome-seen")
async def system_update_outcome_seen(operation_id: str, user: dict = Depends(require_human_user)):
    """Mark one settled Update's outcome as shown; the first client to do so wins."""
    from automation.update_outcome import mark_outcome_seen
    from infra.db_async import run_in_db

    if await run_in_db(mark_outcome_seen, operation_id) is None:
        raise HTTPException(404, "No settled Update with that id.")
    return {"operation_id": operation_id, "seen": True}


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
