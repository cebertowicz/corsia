"""Schema 52 -> 53: no ``proposed`` mark stands without a filed proposal.

Before #2479 an in-turn ``continuity_edit(place, verdict="proposed")`` marked
the entry ``proposed`` with the agent's own destination text, and nothing ever
filed it, so no checkpoint reviewed that lesson again. The database cannot
tell those marks from the worker's: its detail is an issue URL or the backlog
repository, and the in-turn reason ``placed-in-turn`` reached only the server
log. So this step returns every ``proposed`` entry to unmarked. From schema 53
only the worker writes ``proposed``, after filing. An entry the worker already
filed costs one more checkpoint review; filing it again finds the existing
issue by its placement marker instead of opening another.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_52_to_53(conn: sqlite3.Connection) -> None:
    """Unmark every ``proposed`` entry so a checkpoint reviews it again."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(continuity_entries)")}
    if "placement_state" not in columns:
        return
    conn.execute(
        "UPDATE continuity_entries SET placement_state=NULL,"
        "placement_revision=NULL,placement_detail=NULL "
        "WHERE placement_state='proposed'"
    )
