"""Shared scope validation for delegated thread sends."""

from __future__ import annotations

import logging

from infra.conductor import ConductorSettingError, get_conductor
from infra.queries import thread_available_sql, thread_unavailable_reason


WORKSPACE_SCOPE_ID = "workspace"
log = logging.getLogger(__name__)


def scope_id_for_project(project_id: str | None) -> str:
    """Return the stable scope id used by explicit cross-scope delegation."""
    return project_id or WORKSPACE_SCOPE_ID


def scope_label(project_id: str | None) -> str:
    """Return the human/debug label for a scope."""
    return f"project:{project_id}" if project_id else WORKSPACE_SCOPE_ID


def target_scope_authority(project_id: str | None, *, db) -> dict:
    """Return the configured authority slot for a target scope.

    This consumes the existing conductor designation seam; it does not create a
    second authority model for /send.
    """
    try:
        authority_thread_id = get_conductor(
            project_id or WORKSPACE_SCOPE_ID,
            db=db,
        )
    except ConductorSettingError as exc:
        log.warning(
            "Malformed workspace_conductor_session_id setting; "
            "cross-scope repair will require authority reconfiguration: %s",
            exc,
        )
        authority_thread_id = None
    authority_title = None
    authority_slot = "target-scope-conductor"

    if authority_thread_id:
        thread = db.execute(
            "SELECT title FROM threads WHERE id = ? "
            f"AND {thread_available_sql()}",
            (authority_thread_id,),
        ).fetchone()
        if thread:
            authority_title = thread["title"]
        else:
            log.warning(
                "Configured target-scope authority thread is not openable; "
                "cross-scope repair will require authority reconfiguration: %s",
                authority_thread_id,
            )
            authority_thread_id = None

    return {
        "authority_slot": authority_slot,
        "authority_thread_id": authority_thread_id,
        "authority_title": authority_title,
    }


def native_endpoint_gap(
    row: dict,
    *,
    workspace_conductor_id: str | None,
    project_conductor: tuple[str, str] | None = None,
) -> str | None:
    """Return why a thread cannot be a cross-scope native send endpoint.

    A registered agent qualifies while active. A designated conductor
    qualifies through its designation, so a conductor never has to be
    registered a second time as an agent to be reachable. Both designations
    are supplied by the caller as the live value for the scope that owns the
    row: ``workspace_conductor_id`` for the workspace, and
    ``project_conductor`` as that project's ``(project_id, thread_id)``. A
    designation read from some other scope never reaches this rule, so a
    thread designated for one project stays an unregistered endpoint in
    another.
    """
    unavailable = thread_unavailable_reason(row)
    if unavailable:
        return unavailable
    if (row.get("agent_status") or "active") != "active":
        return "agent is not active"
    if workspace_conductor_id and row.get("id") == workspace_conductor_id:
        if row.get("project_id") is None:
            return None
        return "designated workspace conductor is not a workspace thread"
    if project_conductor and row.get("id") == project_conductor[1]:
        # Defensive: callers pair a row with its own project's designation, so
        # a mismatch means a caller wired the wrong scope's authority in.
        if row.get("project_id") == project_conductor[0]:
            return None
        return "designated project conductor is not a thread of its project"
    if not row.get("agent_enabled"):
        return "thread is not a registered agent"
    return None


def cross_scope_send_detail(
    source_project_id: str | None,
    target_project_id: str | None,
    *,
    target_authority: dict,
) -> dict:
    """Return the fail-closed cross-scope /send repair payload."""
    authority = target_authority or {
        "authority_slot": "target-scope-conductor",
        "authority_thread_id": None,
        "authority_title": None,
    }
    has_authority = bool(authority.get("authority_thread_id"))
    workspace_contact = target_project_id is None and has_authority
    repair_action = "configure_target_scope_authority"
    if has_authority:
        repair_action = "coordinate_from_target_scope_authority"
    if workspace_contact:
        repair_action = "send_to_target_scope_authority"
    return {
        "code": "cross_scope_send_blocked",
        "message": (
            "Cross-scope sends to the designated workspace conductor require "
            "the server-bound native delegate_send tool."
            if workspace_contact
            else "Cross-scope /send blocked. Same-scope routing is the default; "
            "coordinate from the target scope authority or configure one first."
        ),
        "source_scope": scope_label(source_project_id),
        "target_scope": scope_label(target_project_id),
        **authority,
        "repair_action": repair_action,
    }


def validate_send_target_scope(
    *,
    source_project_id: str | None,
    target_project_id: str | None,
    target_thread_id: str,
    requested_target_scope_id: str | None,
    target_authority: dict | None,
    trusted_sender_thread_id: str | None = None,
    workspace_authority: dict | None = None,
) -> dict:
    """Validate send scope; only a server-bound native sender may cross scope.

    REST and CLI callers can assert a target scope but cannot supply a trusted
    thread identity, so they remain same-scope only. Native callers supply the
    backend-injected sender and the current workspace designation.
    """
    source_scope_id = scope_id_for_project(source_project_id)
    actual_target_scope_id = scope_id_for_project(target_project_id)
    requested = (requested_target_scope_id or "").strip() or None
    cross_scope = source_scope_id != actual_target_scope_id

    base = {
        "source_scope_id": source_scope_id,
        "actual_target_scope_id": actual_target_scope_id,
        "requested_target_scope_id": requested,
        "cross_scope": cross_scope,
    }

    if requested is not None and requested != actual_target_scope_id:
        return {
            "ok": False,
            "detail": {
                "code": "target_scope_mismatch",
                "message": (
                    "target_scope_id does not match the target thread's scope."
                ),
                **base,
            },
        }

    designated_workspace_id = (workspace_authority or {}).get("authority_thread_id")
    if cross_scope and trusted_sender_thread_id and designated_workspace_id:
        if target_project_id is None and target_thread_id == designated_workspace_id:
            return {"ok": True, **base}
        if source_project_id is None and trusted_sender_thread_id == designated_workspace_id:
            return {"ok": True, **base}

    if cross_scope:
        detail = cross_scope_send_detail(
            source_project_id,
            target_project_id,
            target_authority=target_authority or {},
        )
        detail.update(base)
        return {
            "ok": False,
            "detail": detail,
        }

    return {"ok": True, **base}
