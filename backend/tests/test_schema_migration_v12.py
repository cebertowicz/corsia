"""Schema v12 adds the provider-attempt settlement access path."""

from __future__ import annotations

import sqlite3


def _schema_eleven_database() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute("DROP INDEX IF EXISTS idx_messages_delivery_attempt")
    db.execute("DROP INDEX IF EXISTS idx_messages_active_turn")
    db.execute("PRAGMA user_version=11")
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,execution_epoch,created_at,updated_at) "
        "VALUES ('thread-1','user-1','codex/luna',0,'t0','t0')"
    )
    db.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,created_at,delivery_attempt_id,"
        "write_started_at,input_order) "
        "VALUES ('input-1','thread-1','user','hello','t1','attempt-1','t2',1)"
    )
    db.commit()
    return db


def test_fresh_schema_has_the_ordered_delivery_attempt_index() -> None:
    """A fresh database can seek one attempt in FIFO order."""
    from infra.schema import SCHEMA_SQL, SCHEMA_VERSION

    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA_SQL)

        assert SCHEMA_VERSION == 119
        definition = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_messages_delivery_attempt'"
        ).fetchone()
        assert definition is not None
        normalized = " ".join(str(definition[0]).lower().split())
        assert "(delivery_attempt_id, input_order)" in normalized
        assert "where delivery_attempt_id is not null" in normalized
    finally:
        db.close()


def test_schema_eleven_migrates_without_losing_fenced_input() -> None:
    """The automatic v11-to-v12 step preserves the exact unsettled attempt."""
    from infra.database_migrations import migrate_to_current

    db = _schema_eleven_database()
    try:
        result = migrate_to_current(db, 12)

        assert result.migrated is True
        assert (result.from_version, result.to_version) == (11, 12)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 12
        retained = db.execute(
            "SELECT delivery_attempt_id,write_started_at,input_order "
            "FROM messages WHERE id='input-1'"
        ).fetchone()
        assert tuple(retained) == ("attempt-1", "t2", 1)
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_messages_delivery_attempt'"
        ).fetchone() is not None
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_messages_active_turn'"
        ).fetchone() is None

        repeated = migrate_to_current(db, 12)
        assert repeated.migrated is False
        assert (repeated.from_version, repeated.to_version) == (12, 12)
    finally:
        db.close()
