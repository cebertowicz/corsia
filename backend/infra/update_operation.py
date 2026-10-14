"""Safe API projection of durable Update operation state."""

from __future__ import annotations

import json
import os
from pathlib import Path

_LOG_TAIL_CHARS = 6000


def _failure_log_tail(operation: dict) -> str | None:
    if operation.get("status") != "failed":
        return None
    try:
        detail = json.loads(operation.get("failure_detail") or "{}")
        log_path = detail.get("log_path")
        if not isinstance(log_path, str):
            return None
        from infra.config import LOGS_DIR

        resolved_logs = LOGS_DIR.resolve()
        resolved_path = Path(log_path).resolve()
        resolved_path.relative_to(resolved_logs)
        with resolved_path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - _LOG_TAIL_CHARS))
            return handle.read().decode(errors="replace")[-_LOG_TAIL_CHARS:]
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _stored_changes(operation: dict) -> dict | None:
    try:
        changes = json.loads(operation.get("changes") or "null")
    except (TypeError, ValueError):
        return None
    return changes if isinstance(changes, dict) else None


def project_update_operation(operation: dict, *, activated_by_restart: bool = False) -> dict:
    """Project durable Update state without disclosing arbitrary filesystem paths."""
    blocking = None
    hard_cap_remaining = None
    if operation.get("status") == "in_flight" and operation.get("phase") in {"steering", "draining"}:
        from infra.drain import get_drain_status

        drain_status = get_drain_status()
        blocking = drain_status["activeTurns"]
        hard_cap_remaining = drain_status["hard_cap_remaining"]
    return {
        "operation_id": str(operation["id"]),
        "status": operation["status"],
        "phase": operation.get("phase"),
        "kind": operation.get("kind"),
        "trigger": operation.get("trigger"),
        "target_version": operation.get("target_version"),
        "drain_total": operation.get("drain_total"),
        "blocking": blocking,
        "hard_cap_remaining": hard_cap_remaining,
        "failure_code": operation.get("failure_code"),
        "failure_detail": operation.get("failure_detail"),
        "conductor_told_at": operation.get("conductor_told_at"),
        "log_tail": _failure_log_tail(operation),
        "changes": _stored_changes(operation),
        "outcome_seen_at": operation.get("outcome_seen_at"),
        "activated_by_restart": activated_by_restart,
        "error": operation.get("error"),
        "requested_at": operation.get("requested_at"),
        "settled_at": operation.get("settled_at"),
    }
