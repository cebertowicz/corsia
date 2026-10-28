"""Schema 159 -> 160: rename the persisted confined-worker mode."""

import sqlite3


def migrate_159_to_160(conn: sqlite3.Connection) -> None:
    """Rename thread modes and historical Claude invocation modes in place."""
    thread_columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    identity_columns = {row[1] for row in conn.execute("PRAGMA table_info(artifact_identities)")}
    if "confinement" in thread_columns:
        conn.execute("UPDATE threads SET confinement='untrusted' WHERE confinement='review'")
    if {"metadata", "integration", "type"} <= identity_columns:
        conn.execute(
            "UPDATE artifact_identities "
            "SET metadata=json_set(metadata, '$.claude_invocation.confinement', 'untrusted') "
            "WHERE integration='claude-code' AND type='session' AND json_valid(metadata) "
            "AND json_extract(metadata, '$.claude_invocation.confinement')='review'"
        )
