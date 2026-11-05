"""Schema 175 -> 176: comment tags and per-thread notification state."""


def migrate_175_to_176(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS doc_comment_tags (
        comment_id TEXT NOT NULL REFERENCES doc_comments(id) ON DELETE CASCADE,
        agent_id TEXT NOT NULL,
        agent_name TEXT NOT NULL,
        PRIMARY KEY (comment_id, agent_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS doc_comment_recipient_state (
        root_id TEXT NOT NULL REFERENCES doc_comments(id) ON DELETE CASCADE,
        agent_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        left_thread INTEGER NOT NULL DEFAULT 0,
        notifications INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (root_id, agent_id)
    )""")
