"""Schema 125 -> 126: presence visit rows gain focus spans, without touching old rows."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_125_to_126_presence_focus import migrate_125_to_126
from infra.presence_rollup_schema import PRESENCE_ROLLUP_TABLES, PRESENCE_ROLLUP_TRIGGERS
from infra.schema import SCHEMA_VERSION

_FOCUS_COLUMNS = {"thread_presence": ("focus_spans", "focus_since"), "presence_visits": ("focus_spans",)}


def _columns(db: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    return [(row[1], row[2]) for row in db.execute(f"PRAGMA table_info({table})")]


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    return db


def _wound_back_to_v125() -> sqlite3.Connection:
    """A fresh current database wound back to schema 125, with one open and one finished visit."""
    db = _fresh()
    for trigger in PRESENCE_ROLLUP_TRIGGERS:
        db.execute(f"DROP TRIGGER {trigger}")
    for table in PRESENCE_ROLLUP_TABLES:
        if not table.startswith("idx_"):
            db.execute(f"DROP TABLE {table}")
    for table, columns in _FOCUS_COLUMNS.items():
        for column in columns:
            db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    db.execute("PRAGMA user_version=125")
    db.execute("INSERT INTO threads(id,user_id,created_at,updated_at) VALUES ('t1','u','t','t')")
    db.execute(
        "INSERT INTO thread_presence(user_id,thread_id,present,last_entered_at,last_confirmed_at,updated_at) "
        "VALUES ('u','t1',1,'a','a','a')"
    )
    db.execute(
        "INSERT INTO presence_visits(user_id,surface,thread_id,entered_at,exited_at,seconds,sessions,exit_reason) "
        "VALUES ('u','chat','t1','a','b',60,1,'navigated')"
    )
    db.commit()
    return db


def test_the_step_is_registered_at_125() -> None:
    assert SCHEMA_VERSION == 159
    assert MIGRATIONS[125] is migrate_125_to_126


def test_the_step_matches_a_fresh_schema_and_leaves_old_rows_unfocused() -> None:
    with closing(_fresh()) as fresh, closing(_wound_back_to_v125()) as db:
        migrate_125_to_126(db)

        for table in _FOCUS_COLUMNS:
            assert sorted(_columns(db, table)) == sorted(_columns(fresh, table))
        # NULL spans mean the visit predates focus tracking and counts whole.
        assert db.execute("SELECT focus_spans, focus_since FROM thread_presence").fetchall() == [(None, None)]
        assert db.execute("SELECT focus_spans FROM presence_visits").fetchall() == [(None,)]


def _schema_objects(db: sqlite3.Connection) -> set[tuple[str, str]]:
    return {(row[0], row[1]) for row in db.execute(
        "SELECT type, name FROM sqlite_master WHERE name LIKE '%presence_focus%' OR name LIKE '%presence_visits_rollup%'")}


def test_the_step_adds_the_empty_attention_rollup_the_fresh_schema_has() -> None:
    with closing(_fresh()) as fresh, closing(_wound_back_to_v125()) as db:
        assert _schema_objects(db) == set()
        migrate_125_to_126(db)

        assert _schema_objects(db) == _schema_objects(fresh) != set()
        for table in ("presence_focus_rollup", "presence_focus_sets", "presence_focus_set_members", "presence_focus_rollup_state"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_the_step_is_safe_to_run_again() -> None:
    with closing(_wound_back_to_v125()) as db:
        migrate_125_to_126(db)
        migrate_125_to_126(db)
        assert {name for name, _ in _columns(db, "thread_presence")} >= {"focus_spans", "focus_since"}


def test_the_runner_takes_a_125_database_to_current_and_stays_there() -> None:
    with closing(_wound_back_to_v125()) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 125, SCHEMA_VERSION)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_the_step_leaves_a_database_without_the_presence_tables_alone() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        migrate_125_to_126(db)
        assert db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
