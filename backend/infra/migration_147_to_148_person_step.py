"""Schema 147 -> 148: allow person-step thread update cards.

Runs inside the startup migration transaction. Preserve the existing table's
rows, indexes, triggers, and AUTOINCREMENT high-water mark.
"""

from __future__ import annotations

import sqlite3


def migrate_147_to_148(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='thread_updates'").fetchone()
    if row is None:
        raise RuntimeError("thread_updates table is missing")
    ddl = row[0]
    if "'person_step'" in ddl:
        return
    old = "'done', 'done_follow_up', 'failed', 'closed', 'blocked'"
    if ddl.count(old) != 1:
        raise RuntimeError("unexpected thread_updates kind constraint")
    objects = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='thread_updates' "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name"
    )]
    sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='thread_updates'").fetchone()
    conn.execute(ddl.replace("CREATE TABLE thread_updates", "CREATE TABLE thread_updates_new", 1).replace(
        old, old + ", 'person_step'", 1,
    ))
    conn.execute("INSERT INTO thread_updates_new SELECT * FROM thread_updates")
    conn.execute("DROP TABLE thread_updates")
    conn.execute("ALTER TABLE thread_updates_new RENAME TO thread_updates")
    for statement in objects:
        conn.execute(statement)
    if sequence is not None:
        conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name='thread_updates'", (sequence[0],))
        conn.execute(
            "INSERT INTO sqlite_sequence(name, seq) SELECT 'thread_updates', ? "
            "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='thread_updates')", (sequence[0],),
        )
