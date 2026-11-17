"""Migration 198 -> 199 adds the identity successor index and changes no row."""

from __future__ import annotations

import logging
import sqlite3

from infra.artifact_identity_schema import ARTIFACT_IDENTITY_SCHEMA_SQL
from infra.database_migrations import MIGRATIONS
from infra.migration_198_to_199_identity_successor_index import migrate_198_to_199
from infra.schema import SCHEMA_VERSION

_INDEX = "idx_artifact_identities_successor"


def _old_identity_table() -> sqlite3.Connection:
    """The identity table as 198 had it: the fresh schema without the successor index."""
    db = sqlite3.connect(":memory:")
    db.executescript(ARTIFACT_IDENTITY_SCHEMA_SQL)
    db.execute(f"DROP INDEX {_INDEX}")
    return db


def _identity(db: sqlite3.Connection, identity_id: str, metadata: str | None) -> None:
    db.execute(
        "INSERT INTO artifact_identities (id, integration, type, external_id, metadata, created_at, updated_at)"
        " VALUES (?, 'local', 'file', ?, ?, 't', 't')",
        (identity_id, identity_id, metadata),
    )


def _index_names(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_migration_adds_the_index_keeps_rows_and_is_idempotent(caplog) -> None:
    assert SCHEMA_VERSION == 199 and MIGRATIONS[198] is migrate_198_to_199
    db = _old_identity_table()
    _identity(db, "old", '{"successor_id": "live"}')
    _identity(db, "live", '{"title": "x"}')
    _identity(db, "plain", None)
    _identity(db, "broken", "not json")
    before = db.execute("SELECT * FROM artifact_identities ORDER BY id").fetchall()
    assert _INDEX not in _index_names(db)

    with caplog.at_level(logging.INFO, logger="infra.database_migrations"):
        migrate_198_to_199(db)
    assert _INDEX in _index_names(db)
    assert "elapsed_ms=" in caplog.text
    migrate_198_to_199(db)
    assert db.execute("SELECT * FROM artifact_identities ORDER BY id").fetchall() == before
    plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM artifact_identities "
        "WHERE json_valid(metadata) AND json_extract(metadata, '$.successor_id') = 'live'"
    ).fetchall()
    assert _INDEX in " ".join(row[-1] for row in plan)
    db.close()


def test_migration_on_a_database_without_identities_does_nothing() -> None:
    db = sqlite3.connect(":memory:")
    migrate_198_to_199(db)
    assert db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    db.close()
