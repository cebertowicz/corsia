"""Read stamps and the write-time index of chat-comment answers."""

# What chat draws for a message, decided when it was saved. Two meanings: for an agent message
# that answered comments, its chat text without the answers; '' for a person's comment-only input,
# which chat never draws. The '' row is written by chat_comment_index.place_comments (and the
# 189 -> 190 data step) from chat_comment_index.comment_only, the rule the live broadcast also asks,
# and every chat read honours it through focus_rows.NEVER_DRAWN_FILTER.
CHAT_VISIBLE_CONTENT_SQL = """CREATE TABLE IF NOT EXISTS chat_visible_content (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_visible_content_thread
    ON chat_visible_content(thread_id);
"""

CHAT_COMMENT_SCHEMA_SQL = CHAT_VISIBLE_CONTENT_SQL + """
CREATE TABLE IF NOT EXISTS chat_comments (
    thread_id TEXT NOT NULL,
    target_message_id TEXT NOT NULL,
    user_message_id TEXT NOT NULL,
    cid TEXT NOT NULL,
    created_at TEXT NOT NULL,
    root_user_message_id TEXT,
    root_cid TEXT,
    PRIMARY KEY (user_message_id, cid)
);
CREATE INDEX IF NOT EXISTS idx_chat_comments_target
    ON chat_comments(thread_id, target_message_id);
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
CREATE INDEX IF NOT EXISTS idx_chat_comment_answers_comment
    ON chat_comment_answers(thread_id, user_message_id, cid);
"""
