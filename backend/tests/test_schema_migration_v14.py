"""Schema v14 gives each retained Turn a durable pending-usage handoff."""

from __future__ import annotations

import sqlite3


def _schema_thirteen_database() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute("DROP INDEX IF EXISTS idx_active_turns_pending_usage")
    db.execute("ALTER TABLE active_turns DROP COLUMN usage_receipts_json")
    db.execute("PRAGMA user_version=13")
    db.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) "
        "VALUES ('thread-1','user-1','t0','t0')"
    )
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES ('turn-1','thread-1',0,'test-model','completed','t0','boot-1')"""
    )
    db.commit()
    return db


def test_fresh_schema_has_turn_owned_pending_usage_receipts() -> None:
    from infra.schema import SCHEMA_SQL, SCHEMA_VERSION

    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA_SQL)

        assert SCHEMA_VERSION == 160
        column = next(
            row for row in db.execute("PRAGMA table_info(active_turns)")
            if row[1] == "usage_receipts_json"
        )
        assert column[3] == 1
        assert column[4] == "'{}'"
        definition = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_active_turns_pending_usage'"
        ).fetchone()
        assert definition is not None
        normalized = " ".join(str(definition[0]).lower().split())
        assert "where usage_receipts_json <> '{}'" in normalized
    finally:
        db.close()


def test_schema_thirteen_migrates_without_inventing_historical_receipts() -> None:
    from infra.database_migrations import migrate_to_current

    db = _schema_thirteen_database()
    try:
        result = migrate_to_current(db, 14)

        assert result.migrated is True
        assert (result.from_version, result.to_version) == (13, 14)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 14
        row = db.execute(
            "SELECT usage_receipts_json FROM active_turns WHERE id='turn-1'"
        ).fetchone()
        assert row[0] == "{}"
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_active_turns_pending_usage'"
        ).fetchone() is not None

        repeated = migrate_to_current(db, 14)
        assert repeated.migrated is False
        assert (repeated.from_version, repeated.to_version) == (14, 14)
    finally:
        db.close()
