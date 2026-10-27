"""Identity of a GitHub event created by an observed connected-app call."""
from __future__ import annotations

from typing import Any


def identity(internal_event: str, payload: dict[str, Any]) -> tuple[str, str] | None:
    if internal_event in ("webhook.github.pr_comment", "webhook.github.issue_comment"):
        kind = "issue_comment" if "issue" in payload else "review_comment"
        item = payload.get("comment")
    elif internal_event.startswith("webhook.github.pr_review"):
        kind, item = "review", payload.get("review")
    else:
        return None
    source_id = item.get("id") if isinstance(item, dict) else None
    return (kind, str(source_id)) if type(source_id) is int and source_id > 0 else None
