"""Schema fragment for anchored document comments and suggestions."""

from infra.doc_feedback_schema import DOC_FEEDBACK_SCHEMA_SQL
from infra.doc_review_schema import DOC_REVIEW_SCHEMA_SQL
from infra.migration_111_to_112_image_regions import REGION_NUMBER_INDEX_SQL

#: User comments by time: the corrections scan reads its window's pending comments
#: newest first (``jobs.corrections_scanning``) without reading every comment ever
#: written. Its own constant so step 127 creates it from this one definition.
USER_COMMENTS_BY_TIME_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_doc_comments_user_created
        ON doc_comments(created_at, id) WHERE author_kind='user';
"""

#: Replies by their thread: deciding a suggestion and routing a reply read a thread's
#: replies on the request path. Its own constant so step 180 creates it from this one definition.
REPLIES_BY_PARENT_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_doc_comments_parent
        ON doc_comments(parent_id) WHERE parent_id IS NOT NULL;
"""

DOC_COMMENTS_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS doc_comments (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL,
        parent_id TEXT REFERENCES doc_comments(id),
        author TEXT NOT NULL,
        author_kind TEXT NOT NULL DEFAULT 'user',
        author_thread_id TEXT,
        body TEXT NOT NULL,
        anchor_text TEXT,
        from_off INTEGER,
        to_off INTEGER,
        anchor_prefix TEXT,
        anchor_suffix TEXT,
        review INTEGER NOT NULL DEFAULT 0,
        resolved INTEGER NOT NULL DEFAULT 0,
        acked INTEGER NOT NULL DEFAULT 0,
        replacement TEXT,
        decision TEXT,
        decided_by TEXT,
        decided_by_thread_id TEXT,
        decided_at TEXT,
        decision_event_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        -- An image region in fractions of the image (w = h = 0 is a point), numbered
        -- per image; image_src names the image inside a document, NULL when the
        -- artifact is the image. image_rev is the image's sha256 when placed.
        region_x REAL,
        region_y REAL,
        region_w REAL,
        region_h REAL,
        region_number INTEGER,
        image_rev TEXT,
        image_src TEXT,
        -- When the suggestion's author was told its text was changed; NULL while it can be applied.
        stranded_notified_at TEXT,
        -- 1 when the decision settled several suggestions at once, 0 for a single decision; NULL until decided.
        decided_in_bulk INTEGER,
        insert_after INTEGER NOT NULL DEFAULT 0,
        -- 1 while the suggestion's decision is what resolved it; any other change to `resolved` clears it.
        closed_by_decision INTEGER NOT NULL DEFAULT 0,
        -- The account an agent's tag of the person names: the account that agent works for
        -- (doceditor.comment_create_db). Never an agent tag, which stays a doc_comment_tags row.
        tagged_user_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_doc_comments_artifact
        ON doc_comments(artifact_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_doc_comments_decision_event
        ON doc_comments(decision_event_id)
        WHERE decision_event_id IS NOT NULL;
""" + f"    {REGION_NUMBER_INDEX_SQL};\n" + USER_COMMENTS_BY_TIME_INDEX_SQL + REPLIES_BY_PARENT_INDEX_SQL

# Feedback batches reference doc_comments, so they follow it.
# Tag and recipient state also follow their referenced comments.
COMMENT_RECIPIENT_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS doc_comment_tags (
        comment_id TEXT NOT NULL REFERENCES doc_comments(id) ON DELETE CASCADE,
        agent_id TEXT NOT NULL,
        agent_name TEXT NOT NULL,
        PRIMARY KEY (comment_id, agent_id)
    );
    CREATE TABLE IF NOT EXISTS doc_comment_mentions (
        comment_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        start_offset INTEGER NOT NULL,
        end_offset INTEGER NOT NULL,
        FOREIGN KEY (comment_id, agent_id) REFERENCES doc_comment_tags(comment_id, agent_id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_doc_comment_mentions_comment ON doc_comment_mentions(comment_id, agent_id);
    CREATE TABLE IF NOT EXISTS doc_comment_recipient_state (
        root_id TEXT NOT NULL REFERENCES doc_comments(id) ON DELETE CASCADE,
        agent_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        left_thread INTEGER NOT NULL DEFAULT 0,
        notifications INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (root_id, agent_id)
    );
"""
# An agent's write saved while someone else holds the thread's writer turn (doceditor/writer_line.py).
# A row lives only while it waits: it is deleted when it posts or drops out of line.
# root_id has no foreign key: a deleted thread drops its line with the agent told, not silently.
WRITER_LINE_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS doc_comment_writer_line (
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
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_comment_writer_line_active
        ON doc_comment_writer_line(root_id, agent_thread_id);
    CREATE INDEX IF NOT EXISTS idx_doc_comment_writer_line_root
        ON doc_comment_writer_line(root_id, state, saved_at);
    CREATE INDEX IF NOT EXISTS idx_doc_comment_writer_line_agent
        ON doc_comment_writer_line(agent_thread_id, state);
"""
DOCUMENT_SCHEMA_SQL = (DOC_COMMENTS_SCHEMA_SQL + DOC_FEEDBACK_SCHEMA_SQL + DOC_REVIEW_SCHEMA_SQL
                       + COMMENT_RECIPIENT_SCHEMA_SQL + WRITER_LINE_SCHEMA_SQL)
