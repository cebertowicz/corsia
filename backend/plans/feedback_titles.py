"""The count phrases and the length trim of a document-feedback task's title."""

from __future__ import annotations

from typing import Any

from plans import service


def comment_phrases(items: list[dict[str, Any]]) -> list[str]:
    """Render distinct ordinary, suggested, and reopened comment categories."""
    comments = [item for item in items if item["kind"] == "comment"]
    reopened = sum(item.get("event") == "reopened" for item in comments)
    ordinary = [item for item in comments if item.get("event") != "reopened"]
    suggestions = sum(item.get("replacement") is not None for item in ordinary)
    plain = len(ordinary) - suggestions
    phrases = []
    if plain:
        phrases.append(f"{plain} comment{'s' if plain != 1 else ''}")
    if suggestions:
        phrases.append(f"{suggestions} suggestion{'s' if suggestions != 1 else ''}")
    if reopened:
        phrases.append(f"{reopened} re-opened comment{'s' if reopened != 1 else ''}")
    return phrases


def trim_title(full: str, suffix: str) -> str:
    """The title cut to the node title limit, keeping its suffix."""
    if len(full) <= service.MAX_NODE_TITLE:
        return full
    if len(suffix) < service.MAX_NODE_TITLE:
        room = service.MAX_NODE_TITLE - len(suffix)
        return f"{full[:room - 1]}…{suffix}"
    return f"{full[:service.MAX_NODE_TITLE - 1]}…"
