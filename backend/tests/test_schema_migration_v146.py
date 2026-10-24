"""Schema 145 -> 146: immutable person authorship on document comments."""
from __future__ import annotations

import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_145_to_146_comment_author_user import migrate_145_to_146
from infra.schema import SCHEMA_VERSION


def _v145() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    db.execute("ALTER TABLE doc_comments DROP COLUMN author_user_id")
    db.execute(
        "INSERT INTO doc_comments(id, artifact_id, author, body, created_at, updated_at) "
        "VALUES ('legacy-comment', 'artifact', 'Legacy display name', 'Existing comment', 't', 't')"
    )
    db.execute("PRAGMA user_version=145")
    db.commit()
    return db


def test_v146_registers_comment_person_authorship_migration() -> None:
    assert SCHEMA_VERSION == 146
    assert MIGRATIONS[145] is migrate_145_to_146


def test_v146_migrates_legacy_comments_without_inventing_person_identity() -> None:
    db = _v145()
    try:
        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 145, 146)
        assert db.execute("SELECT author_user_id FROM doc_comments WHERE id='legacy-comment'").fetchone()[0] is None
    finally:
        db.close()


def test_fresh_schema_has_the_optional_person_author_column() -> None:
    db = sqlite3.connect(":memory:")
    try:
        initialize_schema(db)
        assert "author_user_id" in {row[1] for row in db.execute("PRAGMA table_info(doc_comments)")}
    finally:
        db.close()


def test_v146_step_is_safe_to_run_twice() -> None:
    db = _v145()
    try:
        migrate_145_to_146(db)
        migrate_145_to_146(db)
        assert "author_user_id" in {row[1] for row in db.execute("PRAGMA table_info(doc_comments)")}
    finally:
        db.close()
