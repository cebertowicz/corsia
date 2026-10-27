"""Read stamps and the write-time index of chat-comment answers."""

CHAT_COMMENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_comment_views (
    user_id TEXT NOT NULL,
    user_message_id TEXT NOT NULL,
    cid TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (user_message_id, cid, user_id)
);
CREATE TABLE IF NOT EXISTS chat_comment_answers (
    thread_id TEXT NOT NULL,
    user_message_id TEXT NOT NULL,
    cid TEXT NOT NULL,
    assistant_message_id TEXT NOT NULL,
    answered_at TEXT NOT NULL,
    PRIMARY KEY (assistant_message_id, cid)
);
CREATE INDEX IF NOT EXISTS idx_chat_comment_answers_thread_time
    ON chat_comment_answers(thread_id, answered_at);
"""
