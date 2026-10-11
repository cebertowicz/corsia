"""Accepted-upload identity writes that preserve canonical local registration."""

from __future__ import annotations

from typing import Any

from registry import models


def register_accepted_upload(
    thread_id: str,
    attachment: dict,
    file_id: str,
    *,
    source_input_id: str | None = None,
) -> dict[str, Any]:
    """Reuse an upload's registered type and path, or register its URL fallback."""
    existing = models.resolve_upload(file_id)
    return models.register_artifact(
        integration="upload",
        type=existing["type"] if existing is not None else "upload",
        external_id=file_id,
        parent_thread_id=thread_id,
        title=attachment.get("filename", "upload"),
        description=attachment.get("filename"),
        path=None if existing is not None else attachment.get("url"),
        retention=(
            None if existing is not None else models.RETENTION_EPHEMERAL
        ),
        metadata={"mime": attachment.get("mime_type"), "size": attachment.get("size")},
        causal_event_id=(
            f"accepted-send:{thread_id}:{source_input_id}:{file_id}"
            if source_input_id
            else None
        ),
    )
