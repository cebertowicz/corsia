"""Resolve the target and current designation for a native delegated send."""

from __future__ import annotations

import json
from typing import Any

from infra.database import get_db
from infra.delegation_scope import (
    native_endpoint_gap,
    target_scope_authority,
    validate_send_target_scope,
)
from infra.queries import get_thread_for_user


def fetch_send_target(
    thread_id: str,
    user_id: str,
    sender_project_id: str | None = None,
) -> dict | None:
    """Fetch only a same-user target and the live authority of every scope involved.

    ``sender_project_id`` is the sender's own scope as the backend knows it,
    never a caller assertion. A project sender's designation lives in that
    project, so it is read here; a workspace sender is already covered by the
    workspace authority this fetch always returns.
    """
    db = get_db()
    try:
        target = get_thread_for_user(thread_id, user_id, db=db)
        if not target:
            return None
        project_id = target["project_id"] or None
        payload = {
            "thread_row": dict(target),
            "target_authority": target_scope_authority(project_id, db=db),
            "workspace_authority": target_scope_authority(None, db=db),
        }
        sender_project_id = sender_project_id or None
        if sender_project_id is not None and sender_project_id != project_id:
            payload["sender_authority"] = target_scope_authority(
                sender_project_id, db=db,
            )
        return payload
    finally:
        db.close()


def native_send_scope_error(
    *,
    current: dict[str, Any],
    target_payload: dict[str, Any],
    from_thread_id: str,
    target_thread_id: str,
    requested_scope_id: str | None,
    backend_bound: bool,
) -> str | None:
    """Validate scope and live native endpoints before sending a message."""
    target = target_payload["thread_row"]
    cross_scope = (current.get("project_id") or None) != (target.get("project_id") or None)
    scope_check = validate_send_target_scope(
        source_project_id=current.get("project_id") or None,
        target_project_id=target.get("project_id") or None,
        target_thread_id=target_thread_id,
        requested_target_scope_id=requested_scope_id,
        target_authority=target_payload["target_authority"],
        trusted_sender_thread_id=from_thread_id if cross_scope else None,
        workspace_authority=target_payload["workspace_authority"],
    )
    if not scope_check["ok"]:
        return "Error: " + json.dumps(scope_check["detail"], sort_keys=True)
    if not cross_scope:
        return None
    if not backend_bound:
        return "Error: cross-scope send requires an active backend-bound agent turn"
    conductor_id = target_payload["workspace_authority"].get("authority_thread_id")
    # Each endpoint is checked against the designation of the scope that owns
    # it: a project conductor is reachable in its own project and nowhere else.
    sender_project = current.get("project_id") or None
    target_project = target.get("project_id") or None
    endpoints = (
        ("sender", current, sender_project, target_payload.get("sender_authority")),
        ("target", target, target_project, target_payload["target_authority"]),
    )
    for role, row, project_id, authority in endpoints:
        project_conductor = None
        if project_id is not None:
            project_conductor = (project_id, (authority or {}).get("authority_thread_id"))
            if not project_conductor[1]:
                project_conductor = None
        gap = native_endpoint_gap(
            row,
            corsia_agent_id=conductor_id,
            project_conductor=project_conductor,
        )
        if gap:
            return (
                "Error: cross-scope send requires active agent endpoints; "
                f"{role} {row.get('id')}: {gap}"
            )
    return None
