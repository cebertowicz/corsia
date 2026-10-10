"""Connector-neutral routing inputs derived before owner selection."""

from __future__ import annotations

from typing import Any


def structured_tag(payload: dict[str, Any]) -> dict[str, str] | None:
    """Return one explicit event-local structured tag target, if present."""
    raw_tags = payload["corsia_tags"] if "corsia_tags" in payload else payload.get(
        "artifact_tags", [],
    )
    if isinstance(raw_tags, dict):
        raw_tags = [raw_tags]
    if not isinstance(raw_tags, list):
        return None
    for raw in raw_tags:
        if not isinstance(raw, dict):
            continue
        thread_id = raw.get("thread_id")
        if thread_id:
            return {
                "tag": str(raw.get("tag", raw.get("label", "")) or ""),
                "thread_id": str(thread_id),
                "source": str(raw.get("source", "structured-event-metadata")),
            }
    return None
