"""Schema v63 adds the thread update index, additively and idempotently."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_62_to_63_thread_updates import migrate_62_to_63
from infra.schema import SCHEMA_VERSION

_OBJECTS = (
    "thread_updates",
    "idx_thread_updates_thread_time",
    "idx_thread_updates_thread_seq",
    "idx_thread_updates_node",
)


def _shape(db) -> dict[str, str]:
    marks = ",".join("?" for _ in _OBJECTS)
    return {
        name: " ".join(sql.split())
        for name, sql in db.execute(
            f"SELECT name, sql FROM sqlite_master WHERE name IN ({marks})", _OBJECTS,
        )
    }


def _legacy_62(path) -> None:
    """A schema-62 database: today's shape without the update index."""
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP TABLE thread_updates")
        db.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
            " VALUES ('kept','user','Kept','t','t')"
        )
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=62")
        db.commit()


def test_fresh_schema_is_the_current_version_with_the_update_index():
    assert SCHEMA_VERSION == 166
    assert MIGRATIONS[62] is migrate_62_to_63
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert set(_shape(db)) == set(_OBJECTS)


def test_startup_migration_adds_the_fresh_shape_and_keeps_existing_rows(tmp_path):
    path = tmp_path / "v62.db"
    _legacy_62(path)

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 62, SCHEMA_VERSION)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
        migrated = _shape(db)
        assert db.execute("SELECT id FROM threads").fetchall() == [("kept",)]
        assert db.execute("SELECT COUNT(*) FROM thread_updates").fetchone()[0] == 0

    with closing(sqlite3.connect(":memory:")) as fresh:
        initialize_schema(fresh)
        assert migrated == _shape(fresh)


def test_the_step_is_idempotent_on_its_own(tmp_path):
    path = tmp_path / "twice.db"
    _legacy_62(path)
    with closing(sqlite3.connect(path)) as db:
        migrate_62_to_63(db)
        migrate_62_to_63(db)
        assert set(_shape(db)) == set(_OBJECTS)
