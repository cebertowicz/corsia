"""Retiring cancellation metadata preserves legacy rows and installed constraints."""

import sqlite3

import pytest

from infra.doc_review_schema import DOC_REVIEW_SCHEMA_SQL
from infra.migration_132_to_133_review_forks import migrate_132_to_133


def test_v132_cancellation_backfills_state_and_removes_column_preserving_schema():
    db = sqlite3.connect(":memory:")
    try:
        db.executescript("""
            CREATE TABLE artifacts (id TEXT PRIMARY KEY);
            INSERT INTO artifacts VALUES ('doc');
            CREATE TABLE audit (request_id TEXT);
            CREATE TABLE doc_review_requests (
                id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL REFERENCES artifacts(id),
                note TEXT CHECK (note IS NULL OR length(note)<=500),
                client_ids TEXT NOT NULL CHECK (json_valid(client_ids)),
                cancelled_at TEXT,
                answered_batch_id TEXT,
                created_at TEXT NOT NULL,
                CHECK (cancelled_at IS NULL OR answered_batch_id IS NULL)
            );
            CREATE INDEX reviewer_note ON doc_review_requests(note);
            CREATE TRIGGER track_review AFTER INSERT ON doc_review_requests
                BEGIN INSERT INTO audit VALUES (new.id); END;
            INSERT INTO doc_review_requests VALUES ('cancelled','doc','keep','["key"]','cancel-time',NULL,'created');
            INSERT INTO doc_review_requests VALUES ('answered','doc',NULL,'[]',NULL,'batch','created');
            INSERT INTO doc_review_requests VALUES ('pending','doc',NULL,'[]',NULL,NULL,'created');
        """)
        for _ in range(2):
            migrate_132_to_133(db)
        assert "cancelled_at" not in {row[1] for row in db.execute("PRAGMA table_info(doc_review_requests)")}
        assert db.execute("SELECT id,state,state_changed_at FROM doc_review_requests ORDER BY id").fetchall() == [
            ("answered", "submitted", "created"), ("cancelled", "withdrawn", "cancel-time"),
            ("pending", "handed_back", "created"),
        ]
        assert db.execute("SELECT artifact_id,note,client_ids FROM doc_review_requests WHERE id='cancelled'").fetchone() == (
            "doc", "keep", '["key"]',
        )
        assert db.execute("PRAGMA foreign_key_list(doc_review_requests)").fetchone()[2] == "artifacts"
        assert db.execute("SELECT name FROM sqlite_master WHERE name='reviewer_note'").fetchone()
        db.execute("INSERT INTO doc_review_requests(id,artifact_id,client_ids,created_at) VALUES ('new','doc','[]','now')")
        assert db.execute("SELECT request_id FROM audit ORDER BY rowid").fetchall() == [
            ("cancelled",), ("answered",), ("pending",), ("new",),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE doc_review_requests SET note=? WHERE id='new'", ("x" * 501,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE doc_review_requests SET client_ids='invalid' WHERE id='new'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE doc_review_requests SET state='cancelled' WHERE id='new'")
    finally:
        db.close()


def test_fresh_review_schema_has_one_state_fact():
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(DOC_REVIEW_SCHEMA_SQL)
        columns = {row[1] for row in db.execute("PRAGMA table_info(doc_review_requests)")}
        assert "state" in columns and "cancelled_at" not in columns
    finally:
        db.close()
