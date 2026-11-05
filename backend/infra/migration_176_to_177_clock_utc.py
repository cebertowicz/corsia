"""Schema 176 -> 177: clock schedules stored with an offset move to UTC.

Data only. ``due`` and ``settle_run`` compare ``next_run_at`` to a UTC timestamp as text, so a
row that kept its caller's offset would still fire at the wrong instant after the update.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def migrate_176_to_177(conn: sqlite3.Connection) -> None:
    """Rewrite each active clock schedule's next run to its UTC instant.

    Every real database has the table, but the older rungs' migration tests run this on a
    partial schema without it, so the guard stays.
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_schedules'"
    ).fetchone():
        return
    rows = conn.execute(
        "SELECT id,next_run_at FROM automation_schedules WHERE kind='clock' AND state='active'"
    ).fetchall()
    for schedule_id, next_run_at in rows:
        due = datetime.fromisoformat(next_run_at)
        if due.utcoffset():  # A UTC value has a zero offset; one with no offset is read as UTC.
            conn.execute(
                "UPDATE automation_schedules SET next_run_at=? WHERE id=?",
                (due.astimezone(timezone.utc).isoformat(), schedule_id),
            )
