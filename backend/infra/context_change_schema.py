"""Schema for the one passive context-change journal."""

#: A correction note's durable key, so writing a note asks one indexed row
#: instead of reading the thread's whole journal, and the scan's recovery asks
#: whether a unit has any note by its key alone (migration_126_to_127).
NOTE_KEY_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_context_changes_note_key
        ON context_change_journal(json_extract(details_json, '$.note_key'), object_id)
        WHERE object_kind='thread_context' AND json_extract(details_json, '$.note_key') IS NOT NULL;
"""

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
""" + NOTE_KEY_INDEX_SQL
