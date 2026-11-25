"""Serialization helpers for host-access owner snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from host_access.errors import HostAccessSupervisorError


def _encoded(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decoded(value: str) -> dict[str, Any]:
    result = json.loads(value)
    if not isinstance(result, dict):
        raise HostAccessSupervisorError("host-access owner snapshot is invalid")
    return result


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _application_payload(lease: dict[str, Any]) -> dict[str, Any]:
    return {
        "lease_id": lease["id"],
        "application_hash": lease["application_hash"],
        "root_thread_id": lease["root_thread_id"],
        "purpose": lease["purpose"],
        "plan": lease["plan"],
        "duration_seconds": lease["requested_duration_seconds"],
        "expected_resources": lease["expected_resources"],
        "persistent_effects": lease["persistent_effects"],
        "rollback_plan": lease["rollback_plan"],
        "forbidden_effects": lease["forbidden_effects"],
        "requesting_project_id": lease["requesting_project_id"],
        "repo_policy_snapshot": lease["repo_policy_snapshot"],
        "repo_policy_hash": lease["repo_policy_hash"],
        "requested_policy_exceptions": lease[
            "requested_policy_exceptions"
        ],
    }
