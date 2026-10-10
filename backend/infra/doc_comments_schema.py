"""Schema fragment for anchored document comments and suggestions."""

from infra.doc_feedback_schema import DOC_FEEDBACK_SCHEMA_SQL
from infra.doc_review_schema import DOC_REVIEW_SCHEMA_SQL

DOC_COMMENTS_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS doc_comments (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL,
        parent_id TEXT REFERENCES doc_comments(id),
        author TEXT NOT NULL,
        author_kind TEXT NOT NULL DEFAULT 'user',
        author_thread_id TEXT,
        body TEXT NOT NULL,
        anchor_text TEXT,
        from_off INTEGER,
        to_off INTEGER,
        anchor_prefix TEXT,
        anchor_suffix TEXT,
        review INTEGER NOT NULL DEFAULT 0,
        resolved INTEGER NOT NULL DEFAULT 0,
        acked INTEGER NOT NULL DEFAULT 0,
        replacement TEXT,
        decision TEXT,
        decided_by TEXT,
        decided_by_thread_id TEXT,
        decided_at TEXT,
        decision_event_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_doc_comments_artifact
        ON doc_comments(artifact_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_doc_comments_decision_event
        ON doc_comments(decision_event_id)
        WHERE decision_event_id IS NOT NULL;
"""

# Feedback batches reference doc_comments, so they follow it.
DOCUMENT_SCHEMA_SQL = DOC_COMMENTS_SCHEMA_SQL + DOC_FEEDBACK_SCHEMA_SQL + DOC_REVIEW_SCHEMA_SQL
