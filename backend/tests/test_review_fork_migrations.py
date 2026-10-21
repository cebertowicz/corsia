"""Review schema upgrades preserve legacy requests and ordinary batch identity."""

import sqlite3

import pytest

from infra.migration_132_to_133_review_forks import migrate_132_to_133


def test_review_fork_ladder_preserves_legacy_rows_and_is_idempotent():
    db = sqlite3.connect(":memory:")
    try:
        db.executescript("""
            CREATE TABLE doc_review_requests (
                id TEXT PRIMARY KEY, node_id TEXT, cancelled_at TEXT,
                answered_batch_id TEXT, created_at TEXT
            );
            INSERT INTO doc_review_requests VALUES ('legacy', 'review-node', NULL, NULL, '2026-01-01');
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
        assert db.execute("SELECT id,node_id,fork_id,state,state_changed_at,reason FROM doc_review_requests").fetchone() == (
            "legacy", "review-node", None, "handed_back", "2026-01-01", None,
        )
        assert db.execute("SELECT id FROM doc_feedback_batches").fetchone() == ("legacy-batch",)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO doc_feedback_batches VALUES ('duplicate','agent','reviewer','doc','owner','open')")
    finally:
        db.close()


def test_fresh_schema_matches_edited_review_migration_chain():
    from infra.doc_feedback_schema import DOC_FEEDBACK_SCHEMA_SQL
    from infra.doc_review_schema import DOC_REVIEW_REVIEWER_INDEX_SQL, DOC_REVIEW_SCHEMA_SQL

    fresh, upgraded = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    try:
        fresh.executescript(DOC_REVIEW_SCHEMA_SQL + DOC_FEEDBACK_SCHEMA_SQL)
        upgraded.executescript("""
            CREATE TABLE doc_review_requests (
                id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
                requester_kind TEXT NOT NULL CHECK (requester_kind IN ('user','agent')),
                requester_id TEXT NOT NULL,
                reviewer_thread_id TEXT NOT NULL REFERENCES threads(id),
                note TEXT CHECK (note IS NULL OR length(note)<=500),
                client_ids TEXT NOT NULL CHECK (json_valid(client_ids) AND json_type(client_ids)='array'),
                node_id TEXT REFERENCES plan_nodes(id),
                cancelled_at TEXT,
                answered_batch_id TEXT REFERENCES doc_feedback_batches(id),
                created_at TEXT NOT NULL,
                CHECK (cancelled_at IS NULL OR answered_batch_id IS NULL)
            );
            CREATE INDEX idx_doc_review_reviewer ON doc_review_requests(artifact_id,reviewer_thread_id,created_at);
        """ + DOC_REVIEW_REVIEWER_INDEX_SQL + ";" + DOC_FEEDBACK_SCHEMA_SQL)
        migrate_132_to_133(upgraded)
        assert fresh.execute("SELECT name,type FROM sqlite_master ORDER BY name").fetchall() == upgraded.execute(
            "SELECT name,type FROM sqlite_master ORDER BY name",
        ).fetchall()
        for table in ("doc_review_requests", "doc_feedback_batches"):
            # ALTER TABLE appends columns; column order is not part of the named SQL contract.
            assert sorted(row[1:] for row in fresh.execute(f"PRAGMA table_info({table})")) == sorted(
                row[1:] for row in upgraded.execute(f"PRAGMA table_info({table})"))
            assert sorted(row[2:] for row in fresh.execute(f"PRAGMA foreign_key_list({table})")) == sorted(
                row[2:] for row in upgraded.execute(f"PRAGMA foreign_key_list({table})"))
            assert fresh.execute(f"PRAGMA index_list({table})").fetchall() == upgraded.execute(f"PRAGMA index_list({table})").fetchall()
        for connection in (fresh, upgraded):
            connection.execute(
                "INSERT INTO doc_review_requests(id,artifact_id,requester_kind,requester_id,reviewer_thread_id,client_ids,created_at) "
                "VALUES ('new','doc','agent','requester','reviewer','[]','now')",
            )
            assert connection.execute("SELECT state,node_id FROM doc_review_requests").fetchone() == ("handed_back", None)
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("UPDATE doc_review_requests SET state='unknown'")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("UPDATE doc_review_requests SET client_ids='{}'")
    finally:
        fresh.close()
        upgraded.close()
