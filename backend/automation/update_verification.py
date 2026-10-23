"""Packaged Update evidence evaluated by the running backend, never the shell."""

from __future__ import annotations

import json

from automation import self_restart
from automation.restart_settlement import settle_restart_control
from infra import build_info, update_status


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
    target = "failed" if code else "complete"
    if not await settle_restart_control(
        operation_id, target=target, error="The packaged Update did not finish its checks." if code else "",
        failure_code=code, failure_detail=json.dumps(detail),
    ):
        return None
    return await run_in_db(self_restart.operation_by_id, operation_id)
