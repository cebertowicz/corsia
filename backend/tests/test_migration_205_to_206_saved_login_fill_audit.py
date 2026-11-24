"""The fill audit migration matches a fresh database and retains independent history."""

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_205_to_206_saved_login_fill_audit import migrate_205_to_206
from infra.schema import SCHEMA_VERSION


def shape(db):
    return (
        [tuple(row) for row in db.execute("PRAGMA table_info(saved_login_fill_audit)")],
        [tuple(row) for row in db.execute("PRAGMA index_info(idx_saved_login_fill_audit_login_created)")],
        [tuple(row) for row in db.execute("PRAGMA foreign_key_list(saved_login_fill_audit)")],
    )


def test_upgrade_from_205_matches_fresh_database():
    assert SCHEMA_VERSION == 206
    assert MIGRATIONS[205] is migrate_205_to_206
    with closing(sqlite3.connect(":memory:")) as fresh, closing(sqlite3.connect(":memory:")) as old:
        initialize_schema(fresh)
        initialize_schema(old)
        old.execute("DROP TABLE saved_login_fill_audit")
        old.execute("PRAGMA user_version = 205")
        old.commit()
        migrate_to_current(old, SCHEMA_VERSION)
        assert old.execute("PRAGMA user_version").fetchone()[0] == 206
        assert shape(old) == shape(fresh)
        assert [row[2] for row in shape(old)[1]] == ["login_id", "created_at"]
        assert shape(old)[2] == []
        for db in (old, fresh):
            db.execute(
                "INSERT INTO saved_login_fill_audit (created_at, login_id, site, username, thread_id, result) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("now", "login", "https://example.com", "alice", "agent", "unknown"),
            )
