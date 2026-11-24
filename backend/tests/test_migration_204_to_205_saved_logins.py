"""Schema 204 -> 205 adds the saved logins table, the same shape a fresh database gets."""

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS
from infra.migration_204_to_205_saved_logins import migrate_204_to_205
from infra.schema import SCHEMA_VERSION


def _shape(db: sqlite3.Connection) -> tuple:
    columns = tuple(tuple(row) for row in db.execute("PRAGMA table_info(saved_logins)"))
    indexes = tuple(
        (row[2], tuple(column[2] for column in db.execute(f"PRAGMA index_info('{row[1]}')")))
        for row in db.execute("PRAGMA index_list(saved_logins)")
    )
    return columns, indexes


def test_step_is_registered_at_204():
    assert SCHEMA_VERSION >= 205
    assert MIGRATIONS[204] is migrate_204_to_205


def test_the_step_gives_a_204_database_the_fresh_table_and_a_rerun_changes_nothing():
    with closing(sqlite3.connect(":memory:")) as fresh, closing(sqlite3.connect(":memory:")) as old:
        initialize_schema(fresh)
        initialize_schema(old)
        old.execute("DROP TABLE saved_logins")
        assert old.execute("SELECT 1 FROM sqlite_master WHERE name = 'saved_logins'").fetchone() is None
        migrate_204_to_205(old)
        once = list(old.iterdump())
        migrate_204_to_205(old)
        assert list(old.iterdump()) == once
        assert _shape(old) == _shape(fresh)
        assert ("unique", ("site", "username")) in {(("unique" if row[0] else "plain"), row[1]) for row in _shape(old)[1]}


def test_the_table_has_no_password_column():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        names = {row[1] for row in db.execute("PRAGMA table_info(saved_logins)")}
    assert not any("pass" in name or "value" in name or "secret" in name for name in names)
