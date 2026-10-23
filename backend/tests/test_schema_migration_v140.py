"""Migrations that follow schema 139's review-request step."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_139_to_140_review_requesting_node import migrate_139_to_140
from infra.migration_143_to_144_correction_learning import migrate_143_to_144
from infra.schema import SCHEMA_VERSION


def _columns(db) -> list[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(doc_review_requests)")]


def _dev_139() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    db.execute("ALTER TABLE doc_review_requests DROP COLUMN requesting_node_id")
    db.execute("PRAGMA user_version=139")
    db.commit()
    return db


def test_v140_registers_the_requesting_node_step() -> None:
    assert SCHEMA_VERSION == 144
    assert MIGRATIONS[139] is migrate_139_to_140


def test_a_fresh_database_has_the_column() -> None:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    assert "requesting_node_id" in _columns(db)


def test_an_upgraded_database_has_the_column() -> None:
    with closing(_dev_139()) as db:
        assert "requesting_node_id" not in _columns(db)
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.from_version, result.to_version) == (139, 144)
        assert "requesting_node_id" in _columns(db)
        assert migrate_to_current(db, SCHEMA_VERSION).migrated is False


def test_v144_registers_correction_learning_step() -> None:
    assert SCHEMA_VERSION == 144
    assert MIGRATIONS[143] is migrate_143_to_144


def test_migration_adds_per_subject_learning_indexes() -> None:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    db.execute("DROP INDEX idx_learning_loop_triage_subject")
    db.execute("DROP INDEX idx_learning_loop_outcome_subject")
    db.execute("PRAGMA user_version=143")
    assert migrate_to_current(db, 144).migrated
    db.execute("INSERT INTO outcome_events VALUES ('1','a','learning_loop.correction_triage','agent','agent_recorded','t','t','subject',NULL,NULL,NULL,NULL,'{}',NULL)")
    try:
        db.execute("INSERT INTO outcome_events VALUES ('2','b','learning_loop.correction_triage','agent','agent_recorded','t','t','subject',NULL,NULL,NULL,NULL,'{}',NULL)")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("triage uniqueness index was not installed")
