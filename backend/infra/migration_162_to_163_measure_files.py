"""Schema 162 -> 163: retain synced measure-file metadata."""
import sqlite3


def migrate_162_to_163(conn: sqlite3.Connection) -> None:
    """Add metadata without changing existing declaration versions."""
    conn.execute("""CREATE TABLE measure_files (
        owner_thread_id TEXT NOT NULL,
        file_path TEXT NOT NULL,
        file_sha256 TEXT NOT NULL,
        version_attr TEXT,
        synced_at TEXT NOT NULL,
        PRIMARY KEY (owner_thread_id, file_path)
    )""")
