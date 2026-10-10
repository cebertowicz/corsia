"""Schema 48 -> 49: a pane view keeps one mark, ``seen_through``.

The pane now shows only finished work new since the user last looked and
folds it in the same view, so the second watermark, ``folded_through``, has
no reader. Its column is dropped; ``seen_through`` and every row keep their
values. Design: ``shared/planning/plans-fold-since-last-look-2026-09-25.md``.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_48_to_49(conn: sqlite3.Connection) -> None:
    """Drop ``plan_pane_views.folded_through`` when it is present."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(plan_pane_views)")}
    if "folded_through" in columns:
        conn.execute("ALTER TABLE plan_pane_views DROP COLUMN folded_through")
