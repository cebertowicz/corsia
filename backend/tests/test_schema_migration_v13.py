"""Schema v13 adds the exact active-Turn input access path."""

from __future__ import annotations

import sqlite3


def _schema_twelve_database() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute("DROP INDEX IF EXISTS idx_messages_active_turn")
    db.execute("PRAGMA user_version=12")
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,execution_epoch,created_at,updated_at) "
        "VALUES ('thread-1','user-1','codex/luna',0,'t0','t0')"
    )
    db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('turn-1','thread-1',0,'codex/luna','running','t1','boot-1')"
    )
    db.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,created_at,delivery_state,input_order,"
        "active_turn_id) "
        "VALUES ('input-1','thread-1','user','hello','t1','queued',1,'turn-1')"
    )
    db.commit()
    return db


def test_fresh_schema_has_the_ordered_active_turn_index() -> None:
    """A fresh database can seek one Turn's input prefix in FIFO order."""
    from infra.schema import SCHEMA_SQL, SCHEMA_VERSION

    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA_SQL)

        assert SCHEMA_VERSION == 120
        definition = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_messages_active_turn'"
        ).fetchone()
        assert definition is not None
        normalized = " ".join(str(definition[0]).lower().split())
        assert "(active_turn_id, input_order)" in normalized
        assert "where active_turn_id is not null" in normalized
    finally:
        db.close()


def test_schema_twelve_migrates_without_losing_turn_owned_input() -> None:
    """The automatic v12-to-v13 step preserves the exact claimed input."""
    from infra.database_migrations import migrate_to_current

    db = _schema_twelve_database()
    try:
        result = migrate_to_current(db, 13)

        assert result.migrated is True
        assert (result.from_version, result.to_version) == (12, 13)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 13
        retained = db.execute(
            "SELECT delivery_state,input_order,active_turn_id "
            "FROM messages WHERE id='input-1'"
        ).fetchone()
        assert tuple(retained) == ("queued", 1, "turn-1")
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_messages_active_turn'"
        ).fetchone() is not None

        repeated = migrate_to_current(db, 13)
        assert repeated.migrated is False
        assert (repeated.from_version, repeated.to_version) == (13, 13)
    finally:
        db.close()
