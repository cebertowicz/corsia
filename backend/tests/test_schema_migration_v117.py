"""Schema 116 -> 117: schedules may be model-free scripts."""

from __future__ import annotations

import sqlite3

import pytest

from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_116_to_117_script_schedules import migrate_116_to_117
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION

_V116_TABLE = """
    CREATE TABLE automation_schedules (
        id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, scope_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('clock', 'cadence')),
        spec TEXT NOT NULL, next_run_at TEXT NOT NULL, last_run_at TEXT,
        runs INTEGER NOT NULL DEFAULT 0,
        max_runs INTEGER NOT NULL CHECK (max_runs > 0),
        state TEXT NOT NULL DEFAULT 'active'
            CHECK (state IN ('active', 'paused', 'cancelled', 'exhausted')),
        payload_json TEXT, created_by_actor TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_automation_schedules_due
        ON automation_schedules(state, next_run_at);
    CREATE INDEX idx_automation_schedules_scope
        ON automation_schedules(scope_id, next_run_at);
"""


def _columns(db: sqlite3.Connection) -> list[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(automation_schedules)")]


def _indexes(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA index_list(automation_schedules)")}


def test_v117_registers_and_widens_kind_keeping_rows(tmp_path) -> None:
    assert SCHEMA_VERSION == 146 and MIGRATIONS[116] is migrate_116_to_117
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript(_V116_TABLE + """
            CREATE TABLE threads (id TEXT PRIMARY KEY);
            INSERT INTO threads VALUES ('t');
            INSERT INTO automation_schedules VALUES
              ('s1','t','__workspace__','cadence','60','n',NULL,2,5,'active',
               '{}','t','c','u');
            PRAGMA user_version=116;
        """)
    with sqlite3.connect(path) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        row = db.execute(
            "SELECT id,kind,runs,max_runs,script_path,running_since,noted_at "
            "FROM automation_schedules"
        ).fetchone()
        db.execute(
            "INSERT INTO automation_schedules (id,thread_id,scope_id,kind,spec,"
            "next_run_at,max_runs,created_by_actor,created_at,updated_at) "
            "VALUES ('s2','t','w','script','300','n',1,'t','c','u')"
        )
        indexes = _indexes(db)
    assert (result.migrated, result.from_version, result.to_version) == (True, 116, 146)
    assert row == ("s1", "cadence", 2, 5, None, None, None)
    assert {"idx_automation_schedules_due", "idx_automation_schedules_scope"} <= indexes


def test_migrated_table_matches_a_fresh_database(tmp_path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript(_V116_TABLE + "PRAGMA user_version=116;")
    with sqlite3.connect(path) as migrated, sqlite3.connect(":memory:") as fresh:
        migrate_to_current(migrated, SCHEMA_VERSION)
        fresh.executescript(SCHEMA_SQL)
        assert _columns(migrated) == _columns(fresh)
        assert _indexes(migrated) == _indexes(fresh)


def test_script_kind_rejects_unknown_kinds_after_migration(tmp_path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript(_V116_TABLE + "PRAGMA user_version=116;")
        migrate_116_to_117(db)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO automation_schedules (id,thread_id,scope_id,kind,spec,"
                "next_run_at,max_runs,created_by_actor,created_at,updated_at) "
                "VALUES ('x','t','w','weekly','1','n',1,'t','c','u')"
            )
