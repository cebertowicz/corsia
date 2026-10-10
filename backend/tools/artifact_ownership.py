"""Ownership transfer/release tools for registered artifacts.

Split out of tools.artifacts for the file-size ratchet (issue #1030).
"""

from __future__ import annotations

from typing import Any

from infra.database import write_transaction
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from registry import models, service
from registry.ownership_authority import AUTHORITY_PARAMETER
from registry.removal_events import broadcast_artifact_removal_event
from tools.artifact_tool_helpers import causal_turn_id, to_json
from tools.base import Tool
from tools.delegation import _require_context, _tool_schema


class ArtifactRehomeTool(Tool):
    """Explicitly transfer artifact ownership."""

    @property
    def name(self) -> str:
        return "artifact_rehome"

    @property
    def description(self) -> str:
        return "Explicitly transfer artifact ownership through the audited registry path."

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            "integration": {"type": "string"},
            "type": {"type": "string"},
            "external_id": {"type": "string"},
            "new_owner_thread_id": {"type": "string"},
            "authority": AUTHORITY_PARAMETER,
            "reason": {"type": "string"},
            "metadata": {"type": ["object", "null"]},
        }, ["integration", "type", "external_id", "new_owner_thread_id", "reason"])

    async def execute(self, integration: str, external_id: str, new_owner_thread_id: str, reason: str, **kwargs: Any) -> str:
        type_ = kwargs.get("type")
        if not isinstance(type_, str) or not type_.strip():
            return "Error: type is required"
        type_ = type_.strip()
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        actor_thread_id, actor = context
        from registry.ownership import rehome_artifact
        from registry.ownership_authority import (
            can_rehome,
            rehome_source_thread_id,
            thread_is_wakeable,
        )

        authority = kwargs.get("authority") or "current-owner-handoff"
        causal_event_id = causal_turn_id(kwargs)

        def _rehome():
            try:
                with write_transaction(None, scope="tool_rehome_artifact") as db:
                    routing = models.routing_registrations(integration, type_, external_id, db=db)
                    owner = routing.get("owner")
                    if owner is None and routing.get("identity") is None:
                        return "not_found", None
                    current_owner = owner["parent_thread_id"] if owner else None
                    source_thread_id = rehome_source_thread_id(routing, db)
                    source_thread_row = (
                        get_thread_for_user(source_thread_id, actor["user_id"], db=db)
                        if source_thread_id else None
                    )
                    target_thread_row = get_thread_for_user(new_owner_thread_id, actor["user_id"], db=db)
                    actor_thread_row = get_thread_for_user(actor_thread_id, actor["user_id"], db=db)
                    if (
                        actor_thread_row is None
                        or not thread_is_wakeable(target_thread_row)
                        or (source_thread_id and source_thread_row is None)
                    ):
                        return "not_found", None
                    if not can_rehome(
                        authority=authority,
                        actor_thread_id=actor_thread_id,
                        current_owner_thread_id=current_owner,
                        source_thread_row=source_thread_row,
                        target_thread_row=target_thread_row,
                        db=db,
                    ):
                        return "forbidden", {
                            "current_owner_thread_id": current_owner,
                            "requested_owner_thread_id": new_owner_thread_id,
                        }
                    result = rehome_artifact(
                        integration=integration,
                        type=type_,
                        external_id=external_id,
                        new_owner_thread_id=new_owner_thread_id,
                        actor_thread_id=actor_thread_id,
                        authority=authority,
                        reason=reason,
                        source_path="tool:artifact_rehome",
                        causal_event_id=causal_event_id,
                        metadata=kwargs.get("metadata"),
                        removal_event={
                            "actor_kind": "agent",
                            "causal_event_id": causal_event_id,
                            "new_owner_thread_id": new_owner_thread_id,
                        },
                        db=db,
                    )
                    return "ok", result
            except Exception as e:
                return "error", str(e)

        status, result = await run_in_db(_rehome)
        if status == "not_found":
            return "Error: Artifact or thread not found"
        if status == "forbidden":
            return f"Error: Re-home authority denied: {result}"
        if status == "error":
            return f"Error rehoming artifact: {result}"
        event = result.get("removal_event")
        if event is not None:
            broadcast_artifact_removal_event(event)
        result.pop("removal_event", None)
        service.notify_changed(result["artifact"])
        return to_json(result)


class ArtifactReleaseTool(Tool):
    """Explicitly release artifact ownership."""

    @property
    def name(self) -> str:
        return "artifact_release"

    @property
    def description(self) -> str:
        return "Explicitly release artifact ownership through the audited registry path."

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            "integration": {"type": "string"},
            "type": {"type": "string"},
            "external_id": {"type": "string"},
            "authority": AUTHORITY_PARAMETER,
            "reason": {"type": "string"},
            "triage_thread_id": {"type": ["string", "null"]},
            "metadata": {"type": ["object", "null"]},
        }, ["integration", "type", "external_id", "reason"])

    async def execute(self, integration: str, external_id: str, reason: str, **kwargs: Any) -> str:
        type_ = kwargs.get("type")
        if not isinstance(type_, str) or not type_.strip():
            return "Error: type is required"
        type_ = type_.strip()
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        actor_thread_id, actor = context
        from registry.ownership import release_artifact
        from registry.ownership_authority import can_release, thread_is_wakeable

        triage_thread_id = kwargs.get("triage_thread_id")
        authority = kwargs.get("authority") or "current-owner-handoff"
        causal_event_id = causal_turn_id(kwargs)

        def _release():
            try:
                with write_transaction(None, scope="tool_release_artifact") as db:
                    routing = models.routing_registrations(integration, type_, external_id, db=db)
                    owner = routing.get("owner")
                    if owner is None:
                        return "not_found", None
                    current_owner = owner["parent_thread_id"]
                    source_thread_row = get_thread_for_user(current_owner, actor["user_id"], db=db)
                    actor_thread_row = get_thread_for_user(actor_thread_id, actor["user_id"], db=db)
                    triage_thread_row = (
                        get_thread_for_user(triage_thread_id, actor["user_id"], db=db)
                        if triage_thread_id else None
                    )
                    if source_thread_row is None or actor_thread_row is None:
                        return "not_found", None
                    if triage_thread_id and not thread_is_wakeable(triage_thread_row):
                        return "not_found", None
                    if not can_release(
                        authority=authority,
                        actor_thread_id=actor_thread_id,
                        current_owner_thread_id=current_owner,
                        source_thread_row=source_thread_row,
                        triage_thread_row=triage_thread_row,
                        db=db,
                    ):
                        return "forbidden", {
                            "current_owner_thread_id": current_owner,
                            "triage_thread_id": triage_thread_id,
                        }
                    result = release_artifact(
                        integration=integration,
                        type=type_,
                        external_id=external_id,
                        actor_thread_id=actor_thread_id,
                        authority=authority,
                        reason=reason,
                        source_path="tool:artifact_release",
                        triage_thread_id=triage_thread_id,
                        causal_event_id=causal_event_id,
                        metadata=kwargs.get("metadata"),
                        removal_event={
                            "actor_kind": "agent",
                            "causal_event_id": causal_event_id,
                            "triage_thread_id": triage_thread_id,
                        },
                        db=db,
                    )
                    # release drops ownership, so resolve() (owner-scoped) may
                    # return None; the identity itself is unaffected by release.
                    identity = routing["identity"]
                    result["removed_artifact"] = {
                        "id": identity["id"], "type": identity["type"],
                        "title": identity["title"],
                    }
                    return "ok", result
            except Exception as e:
                return "error", str(e)

        status, result = await run_in_db(_release)
        if status == "not_found":
            return "Error: Artifact or thread not found"
        if status == "forbidden":
            return f"Error: Release authority denied: {result}"
        if status == "error":
            return f"Error releasing artifact: {result}"
        result.pop("removed_artifact")
        event = result.pop("removal_event", None)
        if event is not None:
            broadcast_artifact_removal_event(event)
        service.notify_changed(
            dict(integration=integration, type=type_, external_id=external_id)
        )
        return to_json(result)
