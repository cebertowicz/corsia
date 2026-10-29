"""Schema 163 -> 164: retain synced measure-file metadata."""
import sqlite3


def migrate_163_to_164(conn: sqlite3.Connection) -> None:
    """Add metadata without changing existing declaration versions."""
    conn.execute("""CREATE TABLE measure_files (
        owner_thread_id TEXT NOT NULL,
        file_path TEXT NOT NULL,
        file_sha256 TEXT NOT NULL,
        version_attr TEXT,
        synced_at TEXT NOT NULL,
        PRIMARY KEY (owner_thread_id, file_path)
    )""")
    conn.execute("""INSERT INTO measure_files(owner_thread_id,file_path,file_sha256,version_attr,synced_at)
        SELECT owner_thread_id,file_path,file_sha256,NULL,synced_at FROM (
            SELECT owner_thread_id,file_path,file_sha256,synced_at,
                   ROW_NUMBER() OVER (PARTITION BY owner_thread_id,file_path
                                      ORDER BY synced_at DESC, rowid DESC) AS rank
            FROM measure_definitions
        ) WHERE rank=1""")
