"""Backend-owned restart release and resumption for every lifecycle outcome."""

from __future__ import annotations

import asyncio
import json

from automation.restart_resumption import idle_threads_with_held_input, resume_threads_detached
from infra.database import write_transaction


async def abort_restart_control(
    operation_id: str,
    *,
    error: str = "restart cancelled",
    cancelled: bool = False,
    failure_code: str | None = None,
    failure_detail: str | None = None,
) -> bool:
    """Release one failed or cancelled restart without stranding retained work."""
    return await settle_restart_control(
        operation_id, target="cancelled" if cancelled else "failed", error=error,
        failure_code=failure_code, failure_detail=failure_detail,
    )


async def settle_restart_control(
    operation_id: str,
    *,
    target: str,
    error: str = "",
    failure_code: str | None = None,
    failure_detail: str | None = None,
) -> bool:
    """Finish release and resumption before propagating caller cancellation."""
    from subagents.runtime_guard import join_task_through_cancellation

    release = asyncio.create_task(
        _release_and_resume(operation_id, error, target, failure_code, failure_detail)
    )
    cancellation = await join_task_through_cancellation(release)
    released = release.result()
    if cancellation is not None:
        raise cancellation
    return released


def _update_failure_log(operation_id: str, error: str, failure_detail: str | None) -> str:
    """Keep the preparation log or name this operation's lifecycle log."""
    from automation.self_restart import operation_detail
    from infra.config import LOGS_DIR

    detail = operation_detail({"failure_detail": failure_detail})
    if not detail.get("log_path"):
        detail["log_path"] = str(LOGS_DIR / "restart.log")
    return json.dumps(detail)


async def _release_and_resume(operation_id, error, target, failure_code, failure_detail) -> bool:
    from automation import restart
    from automation.update_failure_notice import tell_conductor_detached
    from infra import drain
    from infra.db_async import run_in_db
    from infra.thread_execution import drive_thread

    # Resolve through the public owner so existing cancellation/integration probes
    # observe the same release boundary as startup and Update confirmation.
    kwargs = {} if target in {"failed", "cancelled"} else {"target": target}
    threads = await run_in_db(
        restart._release_restart_control, operation_id, error, target == "cancelled",
        failure_code, failure_detail, **kwargs,
    )
    if threads is None:
        return False
    drain.cancel_drain_projection()
    resume_threads_detached(threads, drive_thread, restart._RESUME_TASKS)
    tell_conductor_detached(operation_id, restart._RESUME_TASKS)
    return True


def release_restart_control(
    operation_id: str,
    error: str,
    cancelled: bool,
    failure_code: str | None,
    failure_detail: str | None,
    *,
    target: str | None = None,
) -> tuple[str, ...] | None:
    """Atomically move the exact operation and gather all work its cutoff held."""
    from automation import self_restart
    from automation.restart_wakes import cancel_operation_registrations
    from infra.restart_authorization import serving_head_sha

    if target not in {"complete", "verifying"} and not cancelled:
        operation = self_restart.operation_by_id(operation_id)
        if operation and operation.get("kind") == "update":
            failure_detail = _update_failure_log(operation_id, error, failure_detail)
    with write_transaction(None, scope="settle_restart_control") as connection:
        if target == "verifying":
            released = self_restart.transition_operation(
                operation_id, expected="resuming", target="verifying", db=connection,
            )
        elif target == "complete":
            released = self_restart.complete_operation(
                operation_id, deployed_sha=serving_head_sha() or None,
                failure_detail=failure_detail, db=connection,
            )
        else:
            released = self_restart.abort_operation(
                operation_id, error=error, cancelled=cancelled, failure_code=failure_code,
                failure_detail=failure_detail, db=connection,
            )
        if not released:
            return None
        # Startup already materializes post-bounce registrations before release.
        woken = () if target else cancel_operation_registrations(operation_id, db=connection)
        return tuple(dict.fromkeys((*woken, *idle_threads_with_held_input(connection))))
