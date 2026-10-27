"""Schema v21 bounds checkpoint-held input release to its eligible rows."""

from __future__ import annotations

import sqlite3


INDEX_NAME = "idx_messages_checkpoint_hold"


def _index_sql(db: sqlite3.Connection) -> str:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (INDEX_NAME,),
    ).fetchone()
    assert row is not None
    return " ".join(str(row[0]).lower().split())


def _schema_twenty_database() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    db.execute("PRAGMA user_version=20")
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,execution_epoch,created_at,updated_at) "
        "VALUES ('thread-1','user-1','codex/luna',7,'t0','t0')"
    )
    db.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,created_at,delivery_state,input_order,"
        "target_execution_epoch,target_model,write_outcome) "
        "VALUES ('input-1','thread-1','user','keep me','t1','queued',1,7,"
        "'codex/luna','proved_no_write')"
    )
    db.commit()
    return db


def test_fresh_and_migrated_schema_share_the_checkpoint_hold_index() -> None:
    from infra.database_migrations import migrate_to_current
    from infra.schema import SCHEMA_SQL, SCHEMA_VERSION

    fresh = sqlite3.connect(":memory:")
    migrated = _schema_twenty_database()
    try:
        fresh.executescript(SCHEMA_SQL)
        result = migrate_to_current(migrated, 21)

        assert SCHEMA_VERSION == 150
        assert result.migrated is True
        assert (result.from_version, result.to_version) == (20, 21)
        assert _index_sql(migrated) == _index_sql(fresh)
        definition = _index_sql(migrated)
        assert "(thread_id, target_model, target_execution_epoch)" in definition
        assert "delivery_state='queued'" in definition
        assert "write_outcome='proved_no_write'" in definition
        assert "active_turn_id is null" in definition
        assert "write_started_at is null" in definition
        assert tuple(migrated.execute(
            "SELECT content,target_model,target_execution_epoch,write_outcome "
            "FROM messages WHERE id='input-1'"
        ).fetchone()) == ("keep me", "codex/luna", 7, "proved_no_write")

        repeated = migrate_to_current(migrated, 21)
        assert repeated.migrated is False
        assert (repeated.from_version, repeated.to_version) == (21, 21)
    finally:
        fresh.close()
        migrated.close()
