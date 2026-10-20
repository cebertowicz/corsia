"""Request-owned review lifecycle; Plans owns handed-back work tasks."""

DOC_REVIEW_REVIEWER_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_doc_review_requests_reviewer "
    "ON doc_review_requests(reviewer_thread_id,created_at)"
)

DOC_REVIEW_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS doc_review_requests (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
        requester_kind TEXT NOT NULL CHECK (requester_kind IN ('user','agent')),
        requester_id TEXT NOT NULL,
        reviewer_thread_id TEXT NOT NULL REFERENCES threads(id),
        note TEXT CHECK (note IS NULL OR length(note)<=500),
        client_ids TEXT NOT NULL CHECK (json_valid(client_ids) AND json_type(client_ids)='array'),
        fork_id TEXT,
        state TEXT NOT NULL DEFAULT 'handed_back'
            CHECK (state IN ('forked','submitted','withdrawn','handed_back','failed')),
        state_changed_at TEXT,
        reason TEXT,
        node_id TEXT REFERENCES plan_nodes(id),
        cancelled_at TEXT,
        answered_batch_id TEXT REFERENCES doc_feedback_batches(id),
        created_at TEXT NOT NULL,
        CHECK (cancelled_at IS NULL OR answered_batch_id IS NULL)
    );
    CREATE INDEX IF NOT EXISTS idx_doc_review_reviewer
        ON doc_review_requests(artifact_id,reviewer_thread_id,created_at);
""" + DOC_REVIEW_REVIEWER_INDEX_SQL + ";"
