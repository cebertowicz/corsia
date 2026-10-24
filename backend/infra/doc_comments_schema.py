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
        -- Immutable authenticated principal for user-authored rows; author stays display text.
        author_user_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_doc_comments_artifact
        ON doc_comments(artifact_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_doc_comments_decision_event
        ON doc_comments(decision_event_id)
        WHERE decision_event_id IS NOT NULL;
""" + f"    {REGION_NUMBER_INDEX_SQL};\n" + USER_COMMENTS_BY_TIME_INDEX_SQL

# Feedback batches reference doc_comments, so they follow it.
DOCUMENT_SCHEMA_SQL = DOC_COMMENTS_SCHEMA_SQL + DOC_FEEDBACK_SCHEMA_SQL + DOC_REVIEW_SCHEMA_SQL
