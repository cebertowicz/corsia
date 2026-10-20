"""Schema 130 -> 131: durable local-only thread scope."""

from __future__ import annotations

import sqlite3

from infra.database_migrations import MIGRATIONS
from infra.migration_130_to_131_local_only_threads import migrate_130_to_131


def test_v130_registers_local_only_thread_scope() -> None:
    assert MIGRATIONS[130] is migrate_130_to_131


def test_v130_adds_the_local_only_column() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")

    migrate_130_to_131(db)

    columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
    assert "local_only" in columns
    assert "local_only_root_thread_id" not in columns
    assert db.execute("SELECT local_only FROM threads").fetchone() is None


def test_v130_skips_a_fixture_with_no_threads_table() -> None:
    db = sqlite3.connect(":memory:")

    migrate_130_to_131(db)

    assert db.execute("SELECT name FROM sqlite_master WHERE name = 'threads'").fetchone() is None


def test_v130_is_safe_for_a_current_shape_rewound_in_a_fixture() -> None:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, local_only INTEGER NOT NULL DEFAULT 0)"
    )

    migrate_130_to_131(db)

    assert len(list(db.execute("PRAGMA table_info(threads)"))) == 2
