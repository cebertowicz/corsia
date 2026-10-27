"""Packaged Update evidence evaluated by the running backend, never the shell."""

from __future__ import annotations

import asyncio
import json

from automation import self_restart
from automation.restart_settlement import settle_restart_control
from infra import build_info, update_status

#: How long a packaged Update may sit in ``verifying`` once its new backend is
#: serving. The updater shell confirms within seconds of health (three calls of
#: at most five seconds each); one still unconfirmed past this has no live shell,
#: for example after the app was restored by hand, and without this the
#: operation would hold its banners and the restart lock indefinitely.
UNCONFIRMED_UPDATE_SECONDS = 60.0


def version_failure(operation: dict) -> tuple[str | None, dict]:
    """Compare stamped versions, claiming an earlier version only with saved proof."""
    target = operation.get("target_version")
    running = build_info.app_version()
    previous = self_restart.previous_version(operation)
    detail = {"target_version": target, "running_version": running, "previous_version": previous}
    try:
        if not isinstance(running, str) or not running:
            return "version_unverified", {**detail, "reason": "running_version_unavailable"}
        target = update_status.normalize_packaged_version(target)
        running = update_status.normalize_packaged_version(running)
    except (TypeError, ValueError):
        return "version_unverified", {**detail, "reason": "unparseable_version"}
    if running == target:
        return None, detail
    try:
        restored = previous is not None and running == update_status.normalize_packaged_version(previous)
    except (TypeError, ValueError):
        restored = False
    return ("rolled_back" if restored else "wrong_version_running"), {**detail, "reason": "mismatch"}


async def confirm_packaged_update(
    operation_id: str, *, healthy: bool, reason: str = "", failure_code: str = "version_unverified",
) -> dict | None:
    """Settle a shell observation through the same release/resume path as abort."""
    from infra.db_async import run_in_db

    operation = await run_in_db(self_restart.operation_by_id, operation_id)
    if operation is None or operation.get("kind") != "update":
        return None
    if operation.get("status") != "in_flight":
        return operation
    if operation.get("phase") != "verifying":
        return None
    code, detail = version_failure(operation)
    if not healthy:
        code = code or failure_code
        detail["reason"] = reason[:500] or "health_check_failed"
    elif reason:
        detail.setdefault("reason", reason[:500])
    target = "failed" if code else "complete"
    if not await settle_restart_control(
        operation_id, target=target, error="The packaged Update did not finish its checks." if code else "",
        failure_code=code, failure_detail=json.dumps(detail),
    ):
        return None
    return await run_in_db(self_restart.operation_by_id, operation_id)


async def settle_unconfirmed_update(logger, *, timeout: float | None = None) -> None:
    """Settle this startup's ``verifying`` Update when no shell confirms it in time.

    The serving backend is the health evidence, so the version check alone
    decides: a match completes it, a mismatch fails it with the usual codes.
    Whichever of this and the shell's confirmation arrives first settles it.
    """
    from infra.db_async import run_in_db

    operation = await run_in_db(self_restart.in_flight_request)
    if operation is None or operation.get("kind") != "update" or operation.get("phase") != "verifying":
        return
    operation_id = str(operation["id"])
    timeout = UNCONFIRMED_UPDATE_SECONDS if timeout is None else timeout
    await asyncio.sleep(timeout)
    current = await run_in_db(self_restart.operation_by_id, operation_id)
    if current is None or current.get("status") != "in_flight" or current.get("phase") != "verifying":
        return
    row = await confirm_packaged_update(operation_id, healthy=True, reason="confirmation_timeout")
    logger.warning(
        "self-restart: Update {} had no confirmation after {:.0f}s; settled {}",
        operation_id, timeout, (row or {}).get("status", "by another owner"),
    )
