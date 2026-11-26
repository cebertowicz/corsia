"""Activity-owned acknowledgements of in-flight email drafts."""

ACTIVITY_DRAFT_ACK_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS activity_draft_acknowledgements (
        user_id TEXT NOT NULL,
        draft_id TEXT NOT NULL REFERENCES action_drafts(id) ON DELETE CASCADE,
        PRIMARY KEY (user_id, draft_id)
    );
"""
