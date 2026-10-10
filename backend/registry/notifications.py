"""Best-effort artifact identity change notifications."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from loguru import logger

from infra import state
from registry import models
from registry.change_notifications import build_artifact_change_messages


def notify_changed(artifact: Mapping[str, Any] | str | None) -> None:
    """Nudge tabs affected by one artifact identity."""
    try:
        if artifact is None:
            return
        found = models.get_artifact(artifact) if isinstance(artifact, str) else artifact
        row = dict(found) if found is not None else None
        if row is None:
            return
        routing = models.routing_registrations(
            str(row["integration"]), str(row["type"]), str(row["external_id"])
        )
        owner = routing.get("owner")
        thread_ids = list(dict.fromkeys(
            str(candidate["parent_thread_id"])
            for candidate in (
                owner, *(routing.get("subscribers") or []), routing.get("triage")
            )
            if candidate is not None
        ))
        if not thread_ids:
            fallback = row.get("parent_thread_id")
            if fallback is None:
                return
            thread_ids = [str(fallback)]
            owner = row
        artifact_id = (routing.get("identity") or row).get("id")
        if artifact_id is None:
            return
        owner_thread_id = str(owner["parent_thread_id"]) if owner else None
        messages = build_artifact_change_messages(
            str(artifact_id),
            owner_thread_id=owner_thread_id,
            affected_thread_ids=thread_ids,
        )
        for user_id, message in messages:
            state.broadcast_to_user(user_id, message)
    except Exception as exc:
        logger.warning(f"registry notify_changed failed: {exc}")
