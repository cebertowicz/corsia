"""Schema 180 -> 181: agent comment writes saved in line for a thread's writer turn."""


def migrate_180_to_181(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS doc_comment_writer_line (
        id TEXT PRIMARY KEY,
        root_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        agent_thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        ops TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting','woken')),
        saved_at TEXT NOT NULL,
        wake_message_id TEXT,
        wakes INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_comment_writer_line_active
        ON doc_comment_writer_line(root_id, agent_thread_id)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_doc_comment_writer_line_root
        ON doc_comment_writer_line(root_id, state, saved_at)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_doc_comment_writer_line_agent
        ON doc_comment_writer_line(agent_thread_id, state)""")
