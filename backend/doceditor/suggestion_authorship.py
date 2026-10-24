"""Canonical person authorship selection for accepted document suggestions."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def accepted_person_author_user_id(
    rows: Iterable[dict[str, Any]], accepted_ids: Iterable[str],
) -> str | None:
    """Return one persisted user principal only for unanimous accepted user winners.

    ``doc_comments.author`` is the persisted principal for user-authored rows.
    Mixed, agent, unknown, missing, and skipped rows retain the accepting agent.
    """
    accepted = set(accepted_ids)
    winners = [row for row in rows if row.get("id") in accepted]
    if not accepted or len(winners) != len(accepted):
        return None
    if any(row.get("author_kind") != "user" for row in winners):
        return None
    authors = {row.get("author") for row in winners}
    if len(authors) != 1:
        return None
    author = authors.pop()
    return author if isinstance(author, str) and author else None
