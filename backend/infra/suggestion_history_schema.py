"""Receipts binding a suggestion decision to the document states it changed."""

SUGGESTION_HISTORY_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS doc_suggestion_history (
        event_id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL,
        suggestion_ids TEXT NOT NULL,
        decision_metadata TEXT NOT NULL,
        before_sha256 TEXT NOT NULL,
        after_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('accepted', 'undone'))
    );
"""
