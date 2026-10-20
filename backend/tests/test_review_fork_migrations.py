"""Review schema upgrades preserve legacy requests and batch uniqueness."""

import sqlite3

import pytest

from infra.migration_132_to_133_review_forks import migrate_132_to_133
from infra.migration_133_to_134_review_feedback_scope import migrate_133_to_134


def test_review_fork_ladder_preserves_legacy_rows_and_is_idempotent():
    db = sqlite3.connect(":memory:")
    try:
        db.executescript("""
            CREATE TABLE doc_review_requests (id TEXT PRIMARY KEY, node_id TEXT);
            INSERT INTO doc_review_requests VALUES ('legacy', 'review-node');
            CREATE TABLE doc_feedback_batches (
                id TEXT PRIMARY KEY, author_kind TEXT, author_id TEXT,
                artifact_id TEXT, recipient_id TEXT, state TEXT
            );
            CREATE UNIQUE INDEX idx_doc_feedback_batches_open
                ON doc_feedback_batches(author_kind,author_id,artifact_id,recipient_id)
                WHERE state='open';
            INSERT INTO doc_feedback_batches VALUES ('legacy-batch','agent','reviewer','doc','owner','open');
        """)
        for _ in range(2):
            migrate_132_to_133(db)
            migrate_133_to_134(db)
        assert db.execute("SELECT * FROM doc_review_requests").fetchone() == (
            "legacy", "review-node", None, None, None,
        )
        assert db.execute("SELECT review_request_id FROM doc_feedback_batches").fetchone() == (None,)
        db.execute("INSERT INTO doc_feedback_batches VALUES ('fork','agent','reviewer','doc','owner','open','request')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO doc_feedback_batches VALUES ('duplicate','agent','reviewer','doc','owner','open','request')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO doc_feedback_batches VALUES ('normal-duplicate','agent','reviewer','doc','owner','open',NULL)")
        db.execute("INSERT INTO doc_feedback_batches VALUES ('other-fork','agent','reviewer','doc','owner','open','other-request')")
    finally:
        db.close()
