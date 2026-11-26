"""Schema 209 -> 210: approved artifact versions."""

import sqlite3


def migrate_209_to_210(conn: sqlite3.Connection) -> None:
    """Create the approval record and indexes in the migration transaction."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS artifact_approvals (
            artifact_id TEXT NOT NULL,
            node_id TEXT,
            source TEXT NOT NULL CHECK (source IN ('bar', 'comment', 'message', 'save', 'suggestion')),
            source_id TEXT,
            doc_sha256 TEXT NOT NULL,
            images TEXT NOT NULL,
            at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_artifact_approvals_node ON artifact_approvals(node_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_artifact_approvals_doc_sha ON artifact_approvals(doc_sha256)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_artifact_approvals_artifact_at ON artifact_approvals(artifact_id, at)")
