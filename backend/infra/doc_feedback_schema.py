"""Schema fragment for durable document-feedback batching."""

DOC_FEEDBACK_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS doc_feedback_batches (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
        author_kind TEXT NOT NULL CHECK (author_kind IN ('agent','user')),
        author_id TEXT NOT NULL,
        recipient_id TEXT NOT NULL,
        delivery_policy TEXT NOT NULL DEFAULT 'immediate'
            CHECK (delivery_policy IN ('immediate','document-held','image-held')),
        state TEXT NOT NULL DEFAULT 'open'
            CHECK (state IN ('open','delivered','undelivered')),
        opened_at TEXT NOT NULL,
        last_item_at TEXT NOT NULL,
        closes_at TEXT NOT NULL,
        closed_reason TEXT CHECK (
            closed_reason IS NULL OR closed_reason IN (
                'submit','quiet','turn_end','send','leave','idle'
            )
        ),
        item_count INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
        delivered_node_id TEXT REFERENCES plan_nodes(id),
        delivery_outcome TEXT CHECK (
            delivery_outcome IS NULL OR delivery_outcome IN ('node','ui','empty')
        ),
        undelivered_reason TEXT,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        author_result_notified_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_feedback_batches_open
        ON doc_feedback_batches(author_kind, author_id, artifact_id, recipient_id, delivery_policy)
        WHERE state='open';
    CREATE INDEX IF NOT EXISTS idx_doc_feedback_batches_due
        ON doc_feedback_batches(state, closes_at);
    CREATE INDEX IF NOT EXISTS idx_doc_feedback_batches_author
        ON doc_feedback_batches(author_kind, author_id, state, updated_at);
    CREATE INDEX IF NOT EXISTS idx_doc_feedback_batches_recipient_open
        ON doc_feedback_batches(recipient_id, author_kind, author_id, delivery_policy, state);

    CREATE TABLE IF NOT EXISTS doc_feedback_batch_items (
        batch_id TEXT NOT NULL REFERENCES doc_feedback_batches(id) ON DELETE CASCADE,
        comment_id TEXT NOT NULL REFERENCES doc_comments(id) ON DELETE CASCADE,
        event TEXT CHECK (event IS NULL OR event='reopened'),
        event_at TEXT,
        event_by TEXT,
        PRIMARY KEY (batch_id, comment_id)
    );
    CREATE INDEX IF NOT EXISTS idx_doc_feedback_batch_items_comment
        ON doc_feedback_batch_items(comment_id);

    CREATE TABLE IF NOT EXISTS doc_feedback_edit_items (
        batch_id TEXT PRIMARY KEY
            REFERENCES doc_feedback_batches(id) ON DELETE CASCADE,
        actor_display TEXT NOT NULL,
        prior_segments_diff TEXT NOT NULL DEFAULT '',
        baseline_sha256 TEXT NOT NULL,
        baseline_text TEXT NOT NULL,
        latest_sha256 TEXT NOT NULL,
        latest_text TEXT NOT NULL,
        save_count INTEGER NOT NULL CHECK (save_count >= 1),
        first_at TEXT NOT NULL,
        last_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS doc_comment_thread_views (
        user_id TEXT NOT NULL,
        root_id TEXT NOT NULL REFERENCES doc_comments(id),
        seen_at TEXT NOT NULL,
        seen_reply_id TEXT NOT NULL,
        PRIMARY KEY (user_id, root_id)
    );
"""
