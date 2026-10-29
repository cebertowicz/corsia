"""Schema 145 -> 146: held comment-delivery lanes remain distinct."""
from __future__ import annotations

import sqlite3

from infra.migration_145_to_146_doc_feedback_delivery_policy import migrate_145_to_146


def _legacy_batches_sql() -> str:
    return """
        DROP INDEX idx_doc_feedback_batches_open;
        DROP INDEX idx_doc_feedback_batches_author;
        DROP INDEX idx_doc_feedback_batches_due;
        DROP TABLE doc_feedback_batches;
        CREATE TABLE doc_feedback_batches (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
            author_kind TEXT NOT NULL CHECK (author_kind IN ('agent','user')),
            author_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
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
        CREATE UNIQUE INDEX idx_doc_feedback_batches_open
            ON doc_feedback_batches(author_kind, author_id, artifact_id, recipient_id)
            WHERE state='open';
        CREATE INDEX idx_doc_feedback_batches_due
            ON doc_feedback_batches(state, closes_at);
        CREATE INDEX idx_doc_feedback_batches_author
            ON doc_feedback_batches(author_kind, author_id, state, updated_at);
    """


def _open_batch_columns(connection: sqlite3.Connection) -> list[str]:
    return [row[2] for row in connection.execute("PRAGMA index_info(idx_doc_feedback_batches_open)")]


def test_v146_migrates_a_v145_batch_table_to_the_fresh_lane_shape() -> None:
    from infra.database import initialize_schema
    from infra.database_migrations import MIGRATIONS, migrate_to_current
    from infra.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == 166 and MIGRATIONS[145] is migrate_145_to_146
    fresh = sqlite3.connect(":memory:")
    db = sqlite3.connect(":memory:")
    try:
        initialize_schema(fresh)
        initialize_schema(db)
        db.executescript(_legacy_batches_sql())
        db.execute(
            """INSERT INTO artifact_identities
               (id, integration, type, external_id, created_at, updated_at)
               VALUES ('document-1', 'local', 'document', 'document-1', 't', 't')"""
        )
        db.execute(
            """INSERT INTO doc_feedback_batches
               (id, artifact_id, author_kind, author_id, recipient_id, state,
                opened_at, last_item_at, closes_at, created_at, updated_at)
               VALUES ('batch-1', 'document-1', 'user', 'user', 'owner', 'open',
                       't', 't', 't', 't', 't')"""
        )
        db.execute("PRAGMA user_version=145")
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 145, 166)
        assert db.execute("SELECT delivery_policy FROM doc_feedback_batches WHERE id='batch-1'").fetchone() == (
            "immediate",
        )
        assert _open_batch_columns(db) == _open_batch_columns(fresh) == [
            "author_kind",
            "author_id",
            "artifact_id",
            "recipient_id",
            "delivery_policy",
        ]
        assert [
            row[2] for row in db.execute("PRAGMA index_info(idx_doc_feedback_batches_recipient_open)")
        ] == [
            row[2] for row in fresh.execute("PRAGMA index_info(idx_doc_feedback_batches_recipient_open)")
        ]
        db.execute(
            """INSERT INTO doc_feedback_batches
               (id, artifact_id, author_kind, author_id, recipient_id, delivery_policy, state,
                opened_at, last_item_at, closes_at, created_at, updated_at)
               VALUES ('batch-2', 'document-1', 'user', 'user', 'owner', 'document-held', 'open',
                       't', 't', 't', 't', 't')"""
        )
    finally:
        fresh.close()
        db.close()
