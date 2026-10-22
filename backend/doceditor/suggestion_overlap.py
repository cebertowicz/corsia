"""Open suggestions, and the rule that an agent never suggests over one.

Two suggestions over the same text cannot both apply, and the person would have to
resolve the conflict. The agent that arrives second is told so at creation; the person
is never refused, because their selection is their own decision.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from doceditor.comment_action_errors import CommentActionError
from doceditor.comment_anchoring import anchored_threads


def is_open_suggestion(row: dict[str, Any]) -> bool:
    """A top-level suggestion nobody has decided or resolved, whoever wrote it."""
    return (row.get("replacement") is not None and not row.get("parent_id")
            and not row.get("decision") and not row.get("resolved"))


def check_agent_suggestion_fits(
    db: sqlite3.Connection, artifact_id: str, path: str | None, start: int, end: int,
) -> None:
    """Refuse an agent's suggestion over ``[start, end)`` (UTF-16 offsets) that overlaps an open one.

    Open suggestions are located the way the person's view locates them, against the
    live text when the document is open and the file otherwise, so a suggestion whose
    text changed (orphaned) no longer blocks. Adjacent spans do not overlap.
    """
    if not path:
        return
    threads, _checked = anchored_threads(artifact_id, path, db, persist=False)
    for row in threads:
        if not is_open_suggestion(row) or row.get("orphaned"):
            continue
        if row["from_off"] is not None and row["from_off"] < end and start < row["to_off"]:
            raise CommentActionError(
                "suggestion_overlaps",
                f"This overlaps an open suggestion ({row['id']}) by {row['author']}. "
                "Reply on that suggestion, or wait until it is accepted or rejected.",
            )
