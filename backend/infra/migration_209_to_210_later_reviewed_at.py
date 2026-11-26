"""Schema 209 -> 210: when a plan's Later pile was last reviewed.

``plans.later_review_at`` holds when the next review is due, and three writers
set it a week out (the first task below the cut, migration 195->196, and a sent
review), so it cannot tell a pile its owner has looked at from one nobody has.
``plans.later_reviewed_at`` is when the last review wake was sent; null means
the pile has never been reviewed. ``plans.later_review`` reads it so that
finishing the work above the cut brings the review forward only when the pile
or the owner's priorities changed since.

Every existing plan reads as never reviewed, so its next early review is sent as
it would have been before. The step runs inside the runner's transaction; it
never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_209_to_210(conn: sqlite3.Connection) -> None:
    """Add the last-review time where the plans table lacks it."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "plans" not in tables:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
    if "later_reviewed_at" not in columns:
        conn.execute("ALTER TABLE plans ADD COLUMN later_reviewed_at TEXT")
