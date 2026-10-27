"""Schema 149 -> 150: insertion suggestions retain their quoted anchor."""

import sqlite3


def migrate_149_to_150(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(doc_comments)")}
    if "insert_after" not in columns:
        conn.execute("ALTER TABLE doc_comments ADD COLUMN insert_after INTEGER NOT NULL DEFAULT 0")
