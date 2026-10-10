"""The rotating review queue: which saved entries one checkpoint offers.

Every active entry of a thread is eligible once it has existed through one
checkpoint, so a checkpoint never reviews an entry it was created or changed
before. Entries are ordered by when they were last reviewed or changed, oldest
first. A checkpoint takes entries off the front for as long as they fit the
room its fork has (``core/placement_review.py``), and an entry it gives a
verdict goes to the back: no entry waits longer than one rotation.

The queue is read from the checkpoint's frozen view, so each candidate carries
the exact revision the fork's ``review`` call is validated against.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from infra.placement_marks import kept_reason


def _instant(value: str | None) -> datetime | None:
    """A stored UTC timestamp, naive or aware; None when absent or unreadable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _previous_install(conn: sqlite3.Connection, thread_id: str) -> datetime | None:
    row = conn.execute(
        "SELECT installed_at FROM checkpoints WHERE thread_id=? AND status='installed' "
        "AND installed_at IS NOT NULL ORDER BY installed_at DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    return _instant(row[0]) if row else None


def _due(
    conn: sqlite3.Connection, thread_id: str, epoch: int, frozen: list[dict[str, Any]],
) -> list[tuple[Any, dict[str, Any]]]:
    """The eligible entries with their sort key and kept reason, front first."""
    live = {
        int(row["entry_id"]): row
        for row in conn.execute(
            "SELECT entry_id,revision,updated_at,reviewed_at,placement_state,"
            "placement_revision,placement_detail FROM continuity_entries "
            "WHERE thread_id=? AND history_epoch=? AND state='active'",
            (thread_id, epoch),
        )
    }
    since = _previous_install(conn, thread_id)
    due = []
    for entry in frozen:
        row = live.get(int(entry["entry_id"]))
        if row is None or int(row["revision"]) != int(entry["revision"]):
            continue
        changed = _instant(row["updated_at"])
        if since is not None and changed is not None and changed > since:
            continue
        if row["placement_state"] == "selected" and row["placement_revision"] == row["revision"]:
            continue
        reviewed = _instant(row["reviewed_at"])
        last = max(filter(None, (changed, reviewed)), default=datetime.min.replace(tzinfo=timezone.utc))
        kept = (
            kept_reason(row["placement_state"], row["placement_detail"])
            if row["placement_revision"] == row["revision"] else None
        )
        key = (last, int(entry["entry_id"]))
        due.append((key, {**entry, "kept_reason": kept, "last_reviewed": last}))
    due.sort(key=lambda item: item[0])
    return due


def review_queue(
    conn: sqlite3.Connection,
    thread_id: str,
    history_epoch: int,
    frozen: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Every entry due for review, front of the queue first."""
    return [entry for _key, entry in _due(conn, thread_id, history_epoch, frozen)]
