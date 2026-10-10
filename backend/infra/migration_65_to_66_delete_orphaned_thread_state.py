"""Schema 65 -> 66: delete checkpoint and continuity rows of deleted threads.

Their ``ON DELETE CASCADE`` never fired, because connections do not enable
``PRAGMA foreign_keys``, so every thread deleted before thread delete called
``delete_thread_checkpoints`` and ``delete_thread_continuity`` left these rows
behind (#2291, #3056). This clears exactly the tables those helpers clear, for
threads that no longer exist. Staged edits have no ``thread_id``: they go when
their checkpoint is missing or belongs to a deleted thread, as the helper
deletes them through their checkpoint.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

_LIVE_CHECKPOINT_IDS = (
    "SELECT id FROM checkpoints WHERE thread_id IN (SELECT id FROM threads)"
)

# Ordered as the helpers delete: staged edits before the checkpoints they key
# on. Each row names the table, the delete, and the tables that delete reads.
_DELETES = (
    (
        "checkpoint_staged_edits",
        f"DELETE FROM checkpoint_staged_edits WHERE checkpoint_id NOT IN ({_LIVE_CHECKPOINT_IDS})",
        ("checkpoints",),
    ),
    (
        "checkpoints",
        "DELETE FROM checkpoints WHERE thread_id NOT IN (SELECT id FROM threads)",
        (),
    ),
    (
        "checkpoint_entry_search",
        "DELETE FROM checkpoint_entry_search WHERE thread_id NOT IN (SELECT id FROM threads)",
        (),
    ),
    (
        "continuity_entries",
        "DELETE FROM continuity_entries WHERE thread_id NOT IN (SELECT id FROM threads)",
        (),
    ),
    (
        "continuity_store",
        "DELETE FROM continuity_store WHERE thread_id NOT IN (SELECT id FROM threads)",
        (),
    ),
)


def _tables(conn: sqlite3.Connection) -> set[str]:
    """Return the names of the tables this database has."""
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }



def migrate_65_to_66(conn: sqlite3.Connection) -> None:
    """Delete deleted threads' checkpoint and continuity rows; log each count.

    Runs inside the migration runner's transaction. A table an older database
    never created has nothing to clear.
    """
    present = _tables(conn)
    for table, sql, reads in _DELETES:
        if not {table, "threads", *reads} <= present:
            continue
        deleted = conn.execute(sql).rowcount
        log.info("migration 65->66 deleted %d orphaned %s rows", deleted, table)
