"""Schema 177 -> 178: comment tags and per-thread notification state."""


def migrate_177_to_178(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS doc_comment_tags (
        comment_id TEXT NOT NULL REFERENCES doc_comments(id) ON DELETE CASCADE,
        agent_id TEXT NOT NULL,
        agent_name TEXT NOT NULL,
        PRIMARY KEY (comment_id, agent_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS doc_comment_mentions (
        comment_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        start_offset INTEGER NOT NULL,
        end_offset INTEGER NOT NULL,
        FOREIGN KEY (comment_id, agent_id) REFERENCES doc_comment_tags(comment_id, agent_id) ON DELETE CASCADE
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS doc_comment_recipient_state (
        root_id TEXT NOT NULL REFERENCES doc_comments(id) ON DELETE CASCADE,
        agent_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        left_thread INTEGER NOT NULL DEFAULT 0,
        notifications INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (root_id, agent_id)
    )""")
