"""Review-request records; Plans remains the only work tracker.

A row stores each fact once: who asked whom, the review task (``node_id``),
when the request was cancelled, and which delivered batch answered it.
Whether the review is still open is never stored; Plans owns it through the
review task's lifecycle (``plans.public.review_is_open``).
"""

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
        requesting_node_id TEXT REFERENCES plan_nodes(id),
        requirements_revision INTEGER,
        node_id TEXT REFERENCES plan_nodes(id),
        cancelled_at TEXT,
        answered_batch_id TEXT REFERENCES doc_feedback_batches(id),
        created_at TEXT NOT NULL,
        CHECK (cancelled_at IS NULL OR answered_batch_id IS NULL)
    );
    CREATE INDEX IF NOT EXISTS idx_doc_review_reviewer
        ON doc_review_requests(artifact_id,reviewer_thread_id,created_at);
""" + DOC_REVIEW_REVIEWER_INDEX_SQL + ";"
