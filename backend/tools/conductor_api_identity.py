"""Preserve the acting thread's attribution on legacy HTTP body contracts."""
from __future__ import annotations

from typing import Any

_ORIGIN_FIELDS = frozenset({
    "from_thread_id", "from_session_id", "actor_thread_id", "actor_session_id",
    "spawned_by", "sender_thread_id", "sender_session_id", "parent_thread_id",
    "as_session_id", "author_thread_id", "actor_thread",
})


def attribution_matches(value: Any, actor: str) -> bool:
    """Refuse claims of another acting sender or creator, including nested bodies."""
    if isinstance(value, dict):
        return all(
            (key not in _ORIGIN_FIELDS or item in (None, "", actor))
            and attribution_matches(item, actor)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(attribution_matches(item, actor) for item in value)
    return True


def bind_delegation_origin(path: str, body: dict | None, actor: str) -> dict | None:
    """Supply the verified sender required by the existing delegation handlers."""
    if body is None:
        return None
    if path == "/api/delegation/threads" or (
        path.startswith("/api/delegation/threads/") and path.endswith(("/send", "/set-model"))
    ):
        return {**body, "from_thread_id": actor}
    if path.startswith("/api/delegation/docs/"):
        return {**body, "as_session_id": actor}
    return body
