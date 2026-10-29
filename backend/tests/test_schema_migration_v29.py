"""Schema v29 adds the per-Turn unattributed-dispatch measurement table."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION


def _legacy_database(tmp_path):
    path = tmp_path / "legacy-unattributed.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute(
            "INSERT INTO threads(id,user_id,model,created_at,updated_at) "
            "VALUES('thread-legacy','user-1','claude-code/opus','t0','t0')"
        )
        db.execute("DROP TABLE plan_unattributed_dispatches")
        db.execute("PRAGMA user_version=28")
        db.commit()
    return path


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 164


def test_fresh_schema_creates_the_measurement_table():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(plan_unattributed_dispatches)")
        }
        assert columns == {
            "turn_id", "thread_id", "call_count", "plan_state", "created_at",
        }


def test_migration_creates_the_table_on_an_existing_database(tmp_path):
    path = _legacy_database(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        result = migrate_to_current(db, 29)
        assert (result.migrated, result.from_version, result.to_version) == (
            True, 28, 29,
        )

        db.execute(
            "INSERT INTO plan_unattributed_dispatches "
            "(turn_id,thread_id,call_count,plan_state,created_at) "
            "VALUES ('turn-1','thread-legacy',2,'none','t1')"
        )
        row = db.execute(
            "SELECT thread_id,call_count,plan_state FROM plan_unattributed_dispatches "
            "WHERE turn_id='turn-1'"
        ).fetchone()
        assert tuple(row) == ("thread-legacy", 2, "none")

        assert not migrate_to_current(db, 29).migrated
