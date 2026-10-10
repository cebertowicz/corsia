"""Shared authority checks for explicit artifact ownership changes."""

from __future__ import annotations

from registry import models
from infra.queries import thread_is_available

# The only values can_rehome and can_release accept; anything else is denied.
OWNERSHIP_AUTHORITIES = (
    "current-owner-handoff",
    "workspace-corsia",
    "blessed-edge-policy",
    "source-scope-conductor",
    "target-scope-conductor",
)

AUTHORITY_PARAMETER = {
    "type": ["string", "null"],
    "enum": [*OWNERSHIP_AUTHORITIES, None],
    "description": (
        "Which rule authorizes the change; not a free-text justification. "
        "Default current-owner-handoff: the caller must be the current owner. "
        "workspace-corsia or blessed-edge-policy: the caller must be the "
        "designated workspace conductor. source-scope-conductor or "
        "target-scope-conductor: the caller must be the designated project "
        "conductor of the source thread's project, and the new owner or "
        "triage thread must share that project."
    ),
}


def same_scope(a, b) -> bool:
    """Return true when two thread rows belong to the same project scope."""
    return (a["project_id"] if a else None) == (b["project_id"] if b else None)


def thread_is_wakeable(thread_row) -> bool:
    """Return true when a target thread can receive ownership routing."""
    return thread_is_available(thread_row)


def _is_workspace_conductor(actor_thread_id: str, db) -> bool:
    from infra import conductor

    return conductor.get_conductor(conductor.WORKSPACE_SCOPE, db=db) == actor_thread_id


def _is_project_conductor(actor_thread_id: str, project_id: str | None, db) -> bool:
    if project_id is None:
        return False
    from infra import conductor

    return conductor.get_conductor(project_id, db=db) == actor_thread_id


def can_rehome(
    *,
    authority: str,
    actor_thread_id: str,
    current_owner_thread_id: str | None,
    source_thread_row,
    target_thread_row,
    db,
) -> bool:
    """Authority gate for an explicit re-home; source scope decides."""
    if authority == "current-owner-handoff":
        return (
            current_owner_thread_id is not None
            and actor_thread_id == current_owner_thread_id
        )
    if authority in {"workspace-corsia", "blessed-edge-policy"}:
        return _is_workspace_conductor(actor_thread_id, db)
    if authority in {"source-scope-conductor", "target-scope-conductor"}:
        if not same_scope(source_thread_row, target_thread_row):
            return False
        project_id = source_thread_row["project_id"] if source_thread_row else None
        return _is_project_conductor(actor_thread_id, project_id, db)
    return False


def can_release(
    *,
    authority: str,
    actor_thread_id: str,
    current_owner_thread_id: str,
    source_thread_row,
    triage_thread_row,
    db,
) -> bool:
    """Authority gate for an explicit release; source scope decides."""
    if authority == "current-owner-handoff":
        if actor_thread_id != current_owner_thread_id:
            return False
        if triage_thread_row is not None and not same_scope(source_thread_row, triage_thread_row):
            return False
        return True
    if authority in {"workspace-corsia", "blessed-edge-policy"}:
        return _is_workspace_conductor(actor_thread_id, db)
    if authority in {"source-scope-conductor", "target-scope-conductor"}:
        if triage_thread_row is not None and not same_scope(source_thread_row, triage_thread_row):
            return False
        project_id = source_thread_row["project_id"] if source_thread_row else None
        return _is_project_conductor(actor_thread_id, project_id, db)
    return False


def rehome_source_thread_id(routing: dict, db) -> str | None:
    """Return the source thread that defines re-home authority."""
    owner = routing.get("owner")
    if owner:
        return owner["parent_thread_id"]
    triage = routing.get("triage")
    if triage:
        return triage["parent_thread_id"]
    identity = routing.get("identity")
    if identity:
        return models.latest_released_owner_thread_id(identity["id"], db=db)
    return None
