"""Schema 184 -> 185: the Later cut, and when a plan reviews it.

An agent needs somewhere to put work it will not do now without inventing a date
for it. ``plan_nodes.later`` is the one owner of "below the cut": auto-selection
skips it and nothing about it wakes its owner, while the row itself stays on the
plan unchanged. ``plans.later_review_at`` is when that plan's next weekly review
of the pile is due; null means none is scheduled.

Both default to today's behaviour: every existing plan has no Later rows and no
review armed, so every new predicate reads as it did before. The step runs inside
the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_184_to_185(conn: sqlite3.Connection) -> None:
    """Add the cut flag and the review time where the plan tables lack them."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "plan_nodes" in tables:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(plan_nodes)")}
        if "later" not in columns:
            conn.execute(
                "ALTER TABLE plan_nodes ADD COLUMN later INTEGER NOT NULL DEFAULT 0 "
                "CHECK (later IN (0,1))"
            )
    if "plans" in tables:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
        if "later_review_at" not in columns:
            conn.execute("ALTER TABLE plans ADD COLUMN later_review_at TEXT")
