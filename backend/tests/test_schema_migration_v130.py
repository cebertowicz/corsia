"""Schema 129 -> 130: durable local-only thread scope."""

from __future__ import annotations

import sqlite3

from infra.database_migrations import MIGRATIONS
from infra.migration_129_to_130_local_only import migrate_129_to_130


def test_v129_registers_local_only_thread_scope() -> None:
    assert MIGRATIONS[129] is migrate_129_to_130


def test_v129_adds_local_only_scope_columns() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")

    migrate_129_to_130(db)

    columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
    assert {"local_only", "local_only_inherited_from_thread_id"} <= columns
    assert db.execute("SELECT local_only FROM threads").fetchone() is None


def test_v129_is_safe_for_a_current_shape_rewound_in_a_fixture() -> None:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, local_only INTEGER NOT NULL DEFAULT 0, "
        "local_only_inherited_from_thread_id TEXT)"
    )

    migrate_129_to_130(db)

    assert len(list(db.execute("PRAGMA table_info(threads)"))) == 3
