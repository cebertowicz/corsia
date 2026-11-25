"""Safe API projection of durable Update operation state."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from infra.log_files import nofollow_log_opener
from infra.update_prepare_output import UTC_TIMESTAMP_PATTERN

_LOG_TAIL_CHARS = 6000
_TIMESTAMP_PREFIX_BYTES = len("2000-01-01T00:00:00Z ")


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
        path = Path(log_path).absolute()
        path.parent.resolve().relative_to(resolved_logs)
        with open(path, "rb", opener=nofollow_log_opener) as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - _LOG_TAIL_CHARS)
            read_start = max(0, start - _TIMESTAMP_PREFIX_BYTES)
            handle.seek(read_start)
            data = handle.read()
            cut = start - read_start
            line_start = data.rfind(b"\n", 0, cut) + 1
            if line_start or read_start == 0:
                prefix_end = line_start + _TIMESTAMP_PREFIX_BYTES
                prefix = data[line_start:prefix_end].decode(errors="replace")
                if line_start < cut < prefix_end and re.fullmatch(rf"{UTC_TIMESTAMP_PATTERN} ", prefix):
                    cut = prefix_end
            tail = data[cut:].decode(errors="replace")[-_LOG_TAIL_CHARS:]
            return re.sub(rf"(?m)^{UTC_TIMESTAMP_PATTERN} ", "", tail)
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
