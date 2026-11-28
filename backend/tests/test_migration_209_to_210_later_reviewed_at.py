"""Schema 209 -> 210 adds when a plan's Later pile was last reviewed."""

from __future__ import annotations

import sqlite3

from infra.database_migrations import MIGRATIONS
from infra.migration_209_to_210_later_reviewed_at import migrate_209_to_210
from infra.schema import SCHEMA_VERSION


def _plans_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE plans (id TEXT PRIMARY KEY, owner_id TEXT, later_review_at TEXT)"
    )
    conn.execute("INSERT INTO plans VALUES ('p', 'owner', '2100-01-01T00:00:00+00:00')")


def test_step_is_registered_and_current() -> None:
    assert SCHEMA_VERSION == 212
    assert MIGRATIONS[209] is migrate_209_to_210


def test_existing_plans_read_as_never_reviewed_and_keep_their_review_time() -> None:
    conn = sqlite3.connect(":memory:")
    _plans_table(conn)
    migrate_209_to_210(conn)
    migrate_209_to_210(conn)
    assert conn.execute("SELECT later_review_at, later_reviewed_at FROM plans").fetchone() == (
        "2100-01-01T00:00:00+00:00", None,
    )


def test_database_without_plans_is_left_alone() -> None:
    conn = sqlite3.connect(":memory:")
    migrate_209_to_210(conn)
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
