"""Schema 182 -> 183: the message number a thread was at when it left a Local only project.

History rows record no project, so a thread that moves out of a Local only project
would otherwise keep reading its own pre-move history from wherever it lands
(design row R3, corsia#713). ``local_only_cut_seq`` marks the boundary; every
message at or below it, and all of the thread's checkpoints, stay behind the wall.

Existing threads start with no cut, and the rule starts here: a thread that had already
left a Local only project before this column existed keeps its pre-move messages and
checkpoints readable from wherever it landed, and nothing recorded those earlier moves,
so they cannot be reconstructed here. The step runs inside the runner's
transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_182_to_183(conn: sqlite3.Connection) -> None:
    """Add the cut column where the threads table lacks it."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    if columns and "local_only_cut_seq" not in columns:
        conn.execute("ALTER TABLE threads ADD COLUMN local_only_cut_seq INTEGER")
