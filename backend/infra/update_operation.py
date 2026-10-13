"""Safe API projection of durable Update operation state."""

from __future__ import annotations

import json
from pathlib import Path


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
        return resolved_path.read_text(errors="replace")[-6000:]
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def project_update_operation(operation: dict) -> dict:
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
        "error": operation.get("error"),
        "requested_at": operation.get("requested_at"),
        "settled_at": operation.get("settled_at"),
    }
