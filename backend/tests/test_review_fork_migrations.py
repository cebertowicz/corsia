"""Review schema upgrades preserve legacy requests and ordinary batch identity."""

import sqlite3

import pytest

from infra.migration_132_to_133_review_forks import migrate_132_to_133
from infra.migration_139_to_140_review_requesting_node import migrate_139_to_140
from test_doc_review_requests import request, review_env  # noqa: F401


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
                artifact_id TEXT, recipient_id TEXT, state TEXT, updated_at TEXT
            );
            CREATE UNIQUE INDEX idx_doc_feedback_batches_open
                ON doc_feedback_batches(author_kind,author_id,artifact_id,recipient_id)
                WHERE state='open';
            INSERT INTO doc_feedback_batches VALUES ('legacy-batch','agent','reviewer','doc','owner','open','2026-01-01');
        """)
        for _ in range(2):
            migrate_132_to_133(db)
        assert db.execute("SELECT id,node_id,fork_id,state,state_changed_at,reason FROM doc_review_requests").fetchone() == (
            "legacy", "review-node", None, "handed_back", "2026-01-01", None,
        )
        assert db.execute("SELECT id FROM doc_feedback_batches").fetchone() == ("legacy-batch",)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO doc_feedback_batches VALUES ('duplicate','agent','reviewer','doc','owner','open','2026-01-01')")
    finally:
        db.close()


def test_migrated_legacy_request_with_a_closed_node_is_listed_as_ended_without_comments(review_env):
    from doceditor import review_requests
    from plans import public

    db, aid = review_env
    row = review_requests.escalate_fork(db, request(db, aid)["id"])
    public.transition_node(row["node_id"], "closed", db=db)
    db.commit()
    _as_legacy_132(db, row)
    listed = review_requests.for_document(db, aid)
    assert [(item["id"], item["state"], item["outcome"], item["comment_count"]) for item in listed] == [
        (row["id"], "handed_back", "ended_without_comments", 0)]


def test_migrated_legacy_answered_request_keeps_its_comments_and_leaves_the_list(review_env):
    from doceditor import review_feedback, review_requests
    from doceditor.review_lifecycle import request_comment_count
    from infra.database import write_transaction
    from test_doc_review_requests import fork_comment

    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    with write_transaction(db, scope="test_legacy_answered_review"):
        review_feedback.submit_fork_feedback(db, row["id"], "reviewer", aid)
    batch = db.execute("SELECT id FROM doc_feedback_batches WHERE state='delivered'").fetchone()["id"]
    _as_legacy_132(db, row, answered_batch_id=batch)
    migrated = db.execute("SELECT * FROM doc_review_requests WHERE id=?", (row["id"],)).fetchone()
    assert migrated["state"] == "submitted"
    assert request_comment_count(db, migrated) == 1
    assert review_requests.for_document(db, aid) == []


def _as_legacy_132(db, row, *, answered_batch_id=None):
    """Rebuild the table as schema 132 stored this request, then run the real migration."""
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DROP TABLE doc_review_requests")
    db.execute(
        "CREATE TABLE doc_review_requests ("
        "id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifact_identities(id), "
        "requester_kind TEXT NOT NULL, requester_id TEXT NOT NULL, "
        "reviewer_thread_id TEXT NOT NULL REFERENCES threads(id), note TEXT, client_ids TEXT NOT NULL, "
        "node_id TEXT REFERENCES plan_nodes(id), cancelled_at TEXT, "
        "answered_batch_id TEXT REFERENCES doc_feedback_batches(id), created_at TEXT NOT NULL, "
        "CHECK (cancelled_at IS NULL OR answered_batch_id IS NULL))"
    )
    db.execute(
        "INSERT INTO doc_review_requests(id,artifact_id,requester_kind,requester_id,reviewer_thread_id,"
        "note,client_ids,node_id,answered_batch_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (row["id"], row["artifact_id"], row["requester_kind"], row["requester_id"], row["reviewer_thread_id"],
         row["note"], row["client_ids"], row["node_id"], answered_batch_id, row["created_at"]),
    )
    migrate_132_to_133(db)
    db.execute("PRAGMA foreign_keys=ON")


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
        migrate_139_to_140(upgraded)
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
