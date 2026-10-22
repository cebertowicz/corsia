"""Schema 124 -> 125: the reviewer gate keeps the head a maintainer approved."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_124_to_125_reviewer_gate_approval import migrate_124_to_125
from infra.schema import SCHEMA_VERSION


def _columns(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(row[1], row[2]) for row in db.execute("PRAGMA table_info(reviewer_gate_approval)")]


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    return db


def _wound_back_to_v124() -> sqlite3.Connection:
    """A fresh current database wound back to its schema-124 shape, with one review receipt."""
    db = _fresh()
    db.execute("DROP TABLE reviewer_gate_approval")
    db.execute("PRAGMA user_version=124")
    db.execute("INSERT INTO reviewer_gate_state(pr_key,reviewed_sha) VALUES ('o/r#1','abc')")
    db.commit()
    return db


def test_the_step_is_registered_at_124() -> None:
    assert SCHEMA_VERSION == 139
    assert MIGRATIONS[124] is migrate_124_to_125


def test_the_step_matches_a_fresh_schema_and_keeps_receipts() -> None:
    with closing(_fresh()) as fresh, closing(_wound_back_to_v124()) as db:
        migrate_124_to_125(db)

        assert _columns(db) == _columns(fresh) == [("pr_key", "TEXT"), ("approved_sha", "TEXT")]
        assert db.execute("SELECT reviewed_sha FROM reviewer_gate_state").fetchall() == [("abc",)]


def test_the_step_is_safe_to_run_again() -> None:
    with closing(_wound_back_to_v124()) as db:
        migrate_124_to_125(db)
        migrate_124_to_125(db)
        assert len(_columns(db)) == 2


def test_the_runner_takes_a_124_database_to_current_and_stays_there() -> None:
    with closing(_wound_back_to_v124()) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 124, SCHEMA_VERSION)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert len(_columns(db)) == 2
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
