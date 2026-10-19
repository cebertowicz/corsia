"""Schema v49: a pane view keeps one mark, ``seen_through``."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.migration_48_to_49_pane_view_one_mark import migrate_48_to_49
from infra.schema import SCHEMA_VERSION

# The table as schema 48 left it.
_V48_TABLE = """
    CREATE TABLE plan_pane_views (
        user_id TEXT NOT NULL,
        thread_id TEXT NOT NULL REFERENCES threads(id),
        seen_through TEXT NOT NULL,
        folded_through TEXT NOT NULL CHECK (folded_through <= seen_through),
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, thread_id)
    )
"""
_SEEN = "2026-09-24T10:00:00.000000+00:00"


def _columns(db) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA table_info(plan_pane_views)")}


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 128


def test_fresh_schema_has_one_mark_and_the_step_leaves_it_alone():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        migrate_48_to_49(db)
        assert _columns(db) == {"user_id", "thread_id", "seen_through", "updated_at"}


def test_startup_migration_drops_the_folded_mark_and_keeps_every_view(tmp_path):
    path = tmp_path / "pane-view-one-mark.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP TABLE plan_pane_views")
        db.execute(_V48_TABLE)
        db.execute(
            "INSERT INTO threads (id, user_id, created_at, updated_at) VALUES ('t', 'u', 'x', 'x')"
        )
        db.execute(
            "INSERT INTO plan_pane_views VALUES ('u', 't', ?, '2026-09-24T09:00:00.000000+00:00', 'x')",
            (_SEEN,),
        )
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=48")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 48, SCHEMA_VERSION)
        assert "folded_through" not in _columns(db)
        assert db.execute("SELECT user_id, thread_id, seen_through FROM plan_pane_views").fetchall() == [
            ("u", "t", _SEEN),
        ]
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
