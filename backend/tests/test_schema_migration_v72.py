"""Schema v72 adds durable document feedback batches and comment views."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_71_to_72_doc_feedback import migrate_71_to_72
from infra.schema import SCHEMA_VERSION

# Schema 82 replaced v72's doc_comment_views with per-thread stamps.
_TABLES = {
    "doc_feedback_batches",
    "doc_feedback_batch_items",
}


def _tables(db) -> set[str]:
    return {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def test_current_schema_retains_v72_feedback_tables_and_indices():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert SCHEMA_VERSION >= 72
        assert _TABLES <= _tables(db)
        batch_columns = {row[1] for row in db.execute("PRAGMA table_info(doc_feedback_batches)")}
        assert {
            "artifact_id", "author_kind", "author_id", "recipient_id", "state",
            "opened_at", "last_item_at", "closes_at", "closed_reason", "item_count",
            "delivered_node_id", "undelivered_reason", "attempts",
        } <= batch_columns
        indices = {
            row[1] for row in db.execute("PRAGMA index_list(doc_feedback_batch_items)")
        }
        assert "idx_doc_feedback_batch_items_comment" in indices


def test_v71_startup_migration_adds_feedback_schema_idempotently():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        db.executescript("""
            DROP TABLE doc_feedback_batch_items;
            DROP TABLE doc_feedback_batches;
            PRAGMA user_version=71;
        """)
        db.commit()

        result = migrate_to_current(db, 72)

        assert MIGRATIONS[71] is migrate_71_to_72
        assert (result.migrated, result.from_version, result.to_version) == (True, 71, 72)
        assert _TABLES <= _tables(db)
        assert not migrate_to_current(db, 72).migrated
