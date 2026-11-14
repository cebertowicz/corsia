"""Replacement provenance and irreversible seen evidence for assistant messages."""

MESSAGE_UPDATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS message_update_state (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    update_kind TEXT CHECK(update_kind IN ('post_update','reminder_fallback','legacy_update')),
    presence_at_post TEXT NOT NULL CHECK(presence_at_post IN ('here','away','never_opened','unknown')),
    seen_at TEXT,
    seen_reason TEXT,
    replaced_by TEXT,
    replaced_at TEXT,
    replace_reason TEXT CHECK(replace_reason IN ('corrects','updates')),
    result_text TEXT,
    result_chars INTEGER,
    CHECK ((replaced_by IS NULL) = (replaced_at IS NULL)),
    CHECK ((seen_at IS NULL) = (seen_reason IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_message_update_thread
    ON message_update_state(thread_id,posted_at,message_id);
CREATE INDEX IF NOT EXISTS idx_message_update_unseen
    ON message_update_state(thread_id,posted_at) WHERE seen_at IS NULL AND replaced_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_message_update_replaced_by ON message_update_state(replaced_by);
CREATE TABLE IF NOT EXISTS message_update_nodes (
    message_id TEXT NOT NULL, node_id TEXT NOT NULL,
    PRIMARY KEY(message_id,node_id)
);
"""


def not_replaced_sql(message_id: str = "id") -> str:
    """SQL predicate hiding replaced rows without renumbering the transcript."""
    return ("NOT EXISTS (SELECT 1 FROM message_update_state replacement "
            f"WHERE replacement.message_id={message_id} AND replacement.replaced_by IS NOT NULL)")
