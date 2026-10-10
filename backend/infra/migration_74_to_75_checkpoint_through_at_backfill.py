"""Schema 74 -> 75: backfill ``through_at`` on checkpoints installed before 74.

#3196: 73 -> 74 added the column and left older rows NULL, so a dormant thread
revived on a pre-change checkpoint still read its current-work and next-step
statements as fresh instructions, which is the case the issue is about. The
value is not lost for those rows: an install writes ``through_at`` from the
boundary message's ``created_at``, and every row keeps that boundary's
``thread_id``, ``through_message_id`` and ``through_seq``. This step copies the
same value the install path would have written, so a backfilled row reads as if
it had been captured at install. A row whose boundary message is gone keeps
NULL and renders no dates line. Rows never installed are left to the install
path, which writes the column itself.

A native thread whose current checkpoint is backfilled pays one cold prefix on
its next turn; a dormant thread's cache is already cold.

Idempotent: it only fills NULLs. A database without either table has nothing to
fill, as in 65 -> 66. Runs inside the runner's transaction; it never commits or
rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_74_to_75(conn: sqlite3.Connection) -> None:
    """Fill ``through_at`` from the boundary message for installed rows lacking it."""
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if not {"checkpoints", "messages"} <= tables:
        return
    conn.execute(
        "UPDATE checkpoints SET through_at=("
        "SELECT m.created_at FROM messages m WHERE m.thread_id=checkpoints.thread_id "
        "AND m.id=checkpoints.through_message_id AND m.seq=checkpoints.through_seq) "
        "WHERE through_at IS NULL AND installed_at IS NOT NULL"
    )
