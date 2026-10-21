"""Schema v73 adds durable edit feedback and explicit delivery outcomes."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_72_to_73_doc_edit_feedback import migrate_72_to_73
from infra.schema import SCHEMA_VERSION


def _columns(db, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}


def test_fresh_schema_is_v73_with_edit_items_and_delivery_outcome():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)

        assert SCHEMA_VERSION == 134
        assert "delivery_outcome" in _columns(db, "doc_feedback_batches")
        assert {
            "batch_id",
            "actor_display",
            "prior_segments_diff",
            "baseline_sha256",
            "baseline_text",
            "latest_sha256",
            "latest_text",
            "save_count",
            "first_at",
            "last_at",
        } <= _columns(db, "doc_feedback_edit_items")


def test_v72_startup_migration_adds_edit_feedback_schema_idempotently():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("DROP TABLE doc_feedback_edit_items")
        db.execute("ALTER TABLE doc_feedback_batches DROP COLUMN delivery_outcome")
        db.execute("PRAGMA user_version=72")
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert MIGRATIONS[72] is migrate_72_to_73
        assert (result.migrated, result.from_version, result.to_version) == (True, 72, SCHEMA_VERSION)
        assert "delivery_outcome" in _columns(db, "doc_feedback_batches")
        assert "batch_id" in _columns(db, "doc_feedback_edit_items")
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_dev_v70_database_upgrades_through_every_document_migration():
    """A database at dev's v70 has none of the train's document schema."""
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.executescript("""
            DROP TABLE doc_feedback_edit_items;
            DROP TABLE doc_feedback_batch_items;
            CREATE TABLE doc_comment_views (
                user_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                viewed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, artifact_id)
            );
            DROP TABLE doc_feedback_batches;
        """)
        for column in ("replacement", "decision", "decided_by", "decided_by_thread_id", "decided_at"):
            db.execute(f"ALTER TABLE doc_comments DROP COLUMN {column}")
        db.execute("PRAGMA user_version=70")
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 70, SCHEMA_VERSION)
        assert {"replacement", "decision", "decided_at"} <= _columns(db, "doc_comments")
        assert "delivery_outcome" in _columns(db, "doc_feedback_batches")
        assert "batch_id" in _columns(db, "doc_feedback_edit_items")
        assert "batch_id" in _columns(db, "doc_feedback_batch_items")
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
