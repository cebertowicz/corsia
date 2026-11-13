"""Schema 191: the removed PR reviewer's receipt tables are gone from fresh and upgraded databases alike."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_190_to_191_drop_reviewer_gate_tables import migrate_190_to_191
from infra.schema import SCHEMA_VERSION

GATE_TABLES = {"reviewer_gate_state", "reviewer_gate_approval", "reviewer_gate_pending"}


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _an_install_that_still_holds_them() -> sqlite3.Connection:
    """A fresh current database wound back to schema 190, holding the three tables and one row in each."""
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    db.execute("CREATE TABLE reviewer_gate_state (pr_key TEXT PRIMARY KEY, reviewed_sha TEXT NOT NULL)")
    db.execute("CREATE TABLE reviewer_gate_approval (pr_key TEXT PRIMARY KEY, approved_sha TEXT NOT NULL)")
    db.execute(
        "CREATE TABLE reviewer_gate_pending (pr_key TEXT NOT NULL, head_sha TEXT NOT NULL, "
        "internal_event TEXT NOT NULL, PRIMARY KEY (pr_key, head_sha))"
    )
    db.execute("INSERT INTO reviewer_gate_state VALUES ('o/r#1','abc')")
    db.execute("INSERT INTO reviewer_gate_approval VALUES ('o/r#1','abc')")
    db.execute("INSERT INTO reviewer_gate_pending VALUES ('o/r#1','abc','webhook.github.check_suite_completed')")
    db.execute("PRAGMA user_version=190")
    db.commit()
    return db


def test_the_step_is_registered_at_190() -> None:
    assert MIGRATIONS[190] is migrate_190_to_191
    assert SCHEMA_VERSION == 191


def test_a_fresh_schema_has_none_of_the_tables() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert not GATE_TABLES & _tables(db)


def test_the_step_drops_the_tables_with_their_rows_and_only_them() -> None:
    with closing(_an_install_that_still_holds_them()) as db, closing(sqlite3.connect(":memory:")) as fresh:
        initialize_schema(fresh)
        migrate_190_to_191(db)
        assert _tables(db) == _tables(fresh)


def test_the_step_is_safe_to_run_again_and_on_a_database_that_never_had_them() -> None:
    with closing(_an_install_that_still_holds_them()) as db:
        migrate_190_to_191(db)
        migrate_190_to_191(db)
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        migrate_190_to_191(db)
        assert not GATE_TABLES & _tables(db)


def test_the_runner_takes_a_190_database_to_current_and_stays_there() -> None:
    with closing(_an_install_that_still_holds_them()) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 190, SCHEMA_VERSION)
        assert not GATE_TABLES & _tables(db)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
