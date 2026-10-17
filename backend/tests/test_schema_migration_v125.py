"""Schema 124 -> 125: focused Measurement intervals are independent of presence."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_124_to_125_measurement_focus import migrate_124_to_125
from infra.schema import SCHEMA_VERSION


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _wound_back_to_v124() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    db.execute("DROP TABLE measurement_focus_intervals")
    db.execute("DROP TABLE measurement_focus_users")
    db.execute("PRAGMA user_version=124")
    db.commit()
    return db


def test_step_is_registered_at_124() -> None:
    assert SCHEMA_VERSION == 125
    assert MIGRATIONS[124] is migrate_124_to_125


def test_migration_adds_measurement_records_without_changing_presence_tables() -> None:
    with closing(_wound_back_to_v124()) as db:
        before = {
            table: [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            for table in ("thread_presence", "presence_visits")
        }

        migrate_124_to_125(db)
        migrate_124_to_125(db)

        assert {"measurement_focus_users", "measurement_focus_intervals"} <= _tables(db)
        assert {
            table: [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            for table in before
        } == before


def test_runner_takes_v124_to_current() -> None:
    with closing(_wound_back_to_v124()) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 124, 125)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
