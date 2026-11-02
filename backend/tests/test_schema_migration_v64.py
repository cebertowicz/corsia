"""Schema v64 adds Mark read/unread to the presence row, additively and idempotently."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_63_to_64_presence_seen import migrate_63_to_64
from infra.schema import SCHEMA_VERSION

_SEEN = {"marked_read_at", "marked_unread_at"}


def _columns(db) -> dict[str, tuple]:
    return {row[1]: tuple(row[1:]) for row in db.execute("PRAGMA table_info(thread_presence)")}


def _legacy_63(path) -> None:
    """A schema-63 database: today's presence row without the seen columns."""
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        for column in sorted(_SEEN):
            db.execute(f"ALTER TABLE thread_presence DROP COLUMN {column}")
        db.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
            " VALUES ('kept','user','Kept','t','t')"
        )
        db.execute(
            "INSERT INTO thread_presence (user_id,thread_id,present,last_entered_at,"
            "last_exited_at,last_confirmed_at,updated_at)"
            " VALUES ('user','kept',0,'2026-09-29T10:00:00','2026-09-29T10:05:00',"
            "'2026-09-29T10:00:00','2026-09-29T10:05:00')"
        )
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=63")
        db.commit()


def test_fresh_schema_is_the_current_version_with_the_seen_columns():
    assert SCHEMA_VERSION == 169
    assert MIGRATIONS[63] is migrate_63_to_64
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert _SEEN <= set(_columns(db))


def test_startup_migration_adds_the_fresh_shape_and_keeps_the_visit(tmp_path):
    path = tmp_path / "v63.db"
    _legacy_63(path)

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 63, SCHEMA_VERSION)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
        migrated = _columns(db)
        kept = db.execute(
            "SELECT last_exited_at, marked_read_at, marked_unread_at FROM thread_presence"
        ).fetchall()
        assert kept == [("2026-09-29T10:05:00", None, None)]

    with closing(sqlite3.connect(":memory:")) as fresh:
        initialize_schema(fresh)
        assert {name: migrated[name] for name in _SEEN} == {
            name: _columns(fresh)[name] for name in _SEEN
        }


def test_the_step_is_idempotent_on_its_own(tmp_path):
    path = tmp_path / "twice.db"
    _legacy_63(path)
    with closing(sqlite3.connect(path)) as db:
        migrate_63_to_64(db)
        migrate_63_to_64(db)
        assert _SEEN <= set(_columns(db))
