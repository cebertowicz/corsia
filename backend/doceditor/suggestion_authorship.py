"""Immutable person authorship for suggestion writes."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def accepted_person_author_user_id(
    rows: Iterable[dict[str, Any]], accepted_ids: Iterable[str],
) -> str | None:
    """Return one persisted person principal only when every accepted winner shares it.

    Display names are intentionally irrelevant: a mixed, multi-person, legacy, or
    all-skipped batch keeps the accepting agent as the durable writer.
    """
    accepted = set(accepted_ids)
    winners = [row for row in rows if row.get("id") in accepted]
    if len(winners) != len(accepted) or any(row.get("author_kind") != "user" for row in winners):
        return None
    author_ids = {row.get("author_user_id") for row in winners}
    if len(author_ids) != 1:
        return None
    author_id = author_ids.pop()
    return author_id if isinstance(author_id, str) and author_id else None
