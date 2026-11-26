"""Persistent records of approved artifact versions."""

APPROVAL_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS artifact_approvals (
        artifact_id TEXT NOT NULL,
        node_id TEXT,
        source TEXT NOT NULL CHECK (source IN ('bar', 'comment', 'message', 'save', 'suggestion')),
        source_id TEXT,
        doc_sha256 TEXT NOT NULL,
        images TEXT NOT NULL,
        at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_artifact_approvals_node ON artifact_approvals(node_id);
    CREATE INDEX IF NOT EXISTS idx_artifact_approvals_doc_sha ON artifact_approvals(doc_sha256);
    CREATE INDEX IF NOT EXISTS idx_artifact_approvals_artifact_at ON artifact_approvals(artifact_id, at);
"""
