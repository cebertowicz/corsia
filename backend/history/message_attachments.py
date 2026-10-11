"""Attachment projection for portable messages, including registered upload paths."""

from __future__ import annotations

from typing import Any

from history.types import PortableHistoryError


_ATTACHMENT_KEYS = (
    "file_id",
    "filename",
    "mime_type",
    "url",
    "is_image",
    "is_document",
    "size",
    "artifact_id",
    "path",
)


def _registered_upload_path(
    file_id: object,
    thread_id: object,
    *,
    db,
) -> str | None:
    """Return one thread-owned upload's canonical path from the existing registry.

    ``artifact_identities`` is core schema, present on every migrated instance;
    an ``OperationalError`` here is a real schema failure and must propagate,
    never be read as "this upload was never registered".
    """
    if not isinstance(file_id, str) or not file_id or not isinstance(thread_id, str):
        return None
    if db is None:
        return None
    from registry import models

    artifact = models.resolve_upload(file_id, db=db)
    path = artifact.get("path") if artifact is not None else None
    if artifact is None or artifact.get("parent_thread_id") != thread_id:
        return None
    return path if isinstance(path, str) and path else None


def attachments(
    meta: dict[str, Any],
    message_id: str,
    *,
    thread_id: str | None = None,
    db=None,
) -> tuple[dict[str, Any], ...]:
    """Project a message's attachment metadata, resolving registered upload paths."""
    raw = meta.get("attachments")
    if raw is None:
        raw = meta.get("artifact_references")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PortableHistoryError(f"message {message_id!r} attachments are malformed")
    result: list[dict[str, Any]] = []
    for attachment in raw:
        if not isinstance(attachment, dict):
            raise PortableHistoryError(
                f"message {message_id!r} attachment is malformed"
            )
        stable = {
            key: attachment[key]
            for key in _ATTACHMENT_KEYS
            if key in attachment and attachment[key] is not None
        }
        if not stable:
            raise PortableHistoryError(
                f"message {message_id!r} attachment has no portable identity"
            )
        upload_path = _registered_upload_path(
            stable.get("file_id"),
            thread_id,
            db=db,
        )
        if upload_path is not None:
            stable["path"] = upload_path
        result.append(stable)
    return tuple(result)
