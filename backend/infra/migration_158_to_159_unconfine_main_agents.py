"""Schema 158 -> 159: review workers cannot be roots or spawn children."""

import sqlite3


def migrate_158_to_159(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    if not {"id", "spawned_by", "confinement", "confined_read_dirs"} <= columns:
        return
    conn.execute(
        "UPDATE threads SET confinement=NULL, confined_read_dirs=NULL "
        "WHERE (confinement IS NOT NULL OR confined_read_dirs IS NOT NULL) "
        "AND (spawned_by IS NULL OR id IN "
        "(SELECT spawned_by FROM threads WHERE spawned_by IS NOT NULL))"
    )
