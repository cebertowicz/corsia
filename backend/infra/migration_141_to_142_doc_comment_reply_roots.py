"""Schema 141 -> 142: normalize document-comment reply parents to their thread roots."""
from __future__ import annotations

import sqlite3


def migrate_141_to_142(conn: sqlite3.Connection) -> None:
    """Re-root every stored reply that still points at a reply, without following missing parents.

    One update moves a row one level toward its root. The row count bounds the
    loop so malformed cycles cannot make startup migration unbounded.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "doc_comments" not in tables:
        return
    row_count = conn.execute("SELECT COUNT(*) FROM doc_comments").fetchone()[0]
    for _ in range(row_count):
        changed = conn.execute(
            """UPDATE doc_comments AS reply
               SET parent_id=(
                   SELECT parent.parent_id FROM doc_comments AS parent
                   WHERE parent.id=reply.parent_id
               )
               WHERE reply.parent_id IS NOT NULL
                 AND EXISTS (
                     SELECT 1 FROM doc_comments AS parent
                     WHERE parent.id=reply.parent_id AND parent.parent_id IS NOT NULL
                 )"""
        ).rowcount
        if changed == 0:
            return
