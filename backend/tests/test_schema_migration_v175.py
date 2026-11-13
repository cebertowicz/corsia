"""Schema 174 -> 175: the reviewer gate keeps runs a restart may have cut off."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_174_to_175_reviewer_gate_pending import migrate_174_to_175
from infra.schema import SCHEMA_VERSION


def _columns(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(row[1], row[2]) for row in db.execute("PRAGMA table_info(reviewer_gate_pending)")]


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    return db


def _wound_back_to_v174() -> sqlite3.Connection:
    """A fresh current database wound back to its schema-174 shape, with one review receipt."""
    db = _fresh()
    db.execute("DROP TABLE reviewer_gate_pending")
    db.execute("PRAGMA user_version=174")
    db.execute("INSERT INTO reviewer_gate_state(pr_key,reviewed_sha) VALUES ('o/r#1','abc')")
    db.commit()
    return db


def test_the_step_is_registered_at_174() -> None:
    assert SCHEMA_VERSION == 190
    assert MIGRATIONS[174] is migrate_174_to_175


def test_the_step_matches_a_fresh_schema_and_keeps_receipts() -> None:
    with closing(_fresh()) as fresh, closing(_wound_back_to_v174()) as db:
        migrate_174_to_175(db)

        assert _columns(db) == _columns(fresh) == [
            ("pr_key", "TEXT"), ("head_sha", "TEXT"), ("internal_event", "TEXT"),
        ]
        assert db.execute("SELECT reviewed_sha FROM reviewer_gate_state").fetchall() == [("abc",)]


def test_the_step_is_safe_to_run_again() -> None:
    with closing(_wound_back_to_v174()) as db:
        migrate_174_to_175(db)
        migrate_174_to_175(db)
        assert len(_columns(db)) == 3


def test_the_runner_takes_a_174_database_to_current_and_stays_there() -> None:
    with closing(_wound_back_to_v174()) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 174, SCHEMA_VERSION)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert len(_columns(db)) == 3
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
