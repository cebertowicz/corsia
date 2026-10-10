"""Schema for the one passive context-change journal."""

CONTEXT_CHANGE_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS context_change_journal (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        object_kind TEXT NOT NULL,
        object_id TEXT NOT NULL,
        change_kind TEXT NOT NULL,
        summary TEXT NOT NULL,
        details_json TEXT,
        actor_thread_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_context_changes_object
        ON context_change_journal(object_kind, object_id, seq);
"""
