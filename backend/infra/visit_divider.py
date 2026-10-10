"""Bounded transcript divider derivation shared by presence and update reads."""

from __future__ import annotations

import sqlite3

from infra.presence_time import iso, parse, seen_boundary

#: The maximum number of newest transcript rows a visit divider inspects.
DIVIDER_SCAN_LIMIT = 1_000


def divider_from_row(
    db: sqlite3.Connection, row: sqlite3.Row | None, thread_id: str,
) -> dict | None:
    """Build one divider from a committed presence row.

    The newest-first scan is capped, so a divider never walks an unbounded old
    transcript. ``truncated`` means every scanned row was newer than the
    boundary, so messages preceding ``first_seq`` are also unseen.
    """
    since = seen_boundary(row)
    if since is None:
        return None
    first_seq: int | None = None
    rows = list(db.execute(
        """SELECT seq,created_at FROM messages
             WHERE thread_id=? AND seq IS NOT NULL
             ORDER BY seq DESC LIMIT ?""",
        (thread_id, DIVIDER_SCAN_LIMIT),
    ))
    for message in rows:
        created_at = message["created_at"]
        if created_at is None or iso(parse(created_at)) <= since:
            break
        first_seq = int(message["seq"])
    else:
        divider = {"since": since, "first_seq": first_seq}
        if len(rows) == DIVIDER_SCAN_LIMIT and first_seq is not None:
            divider["truncated"] = True
        return divider
    return {"since": since, "first_seq": first_seq}
