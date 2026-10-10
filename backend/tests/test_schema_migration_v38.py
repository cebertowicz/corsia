"""Schema v38 widens checkpoint triggers without losing retained rows."""

from __future__ import annotations

import sqlite3


def test_agent_trigger_migration_preserves_rows_and_sequence():
    from infra.database_migrations import MIGRATIONS, _CHECKPOINTS_38_DDL

    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        db.executescript(_CHECKPOINTS_38_DDL.replace(",'agent'", ""))
        db.execute("ALTER TABLE checkpoints_new RENAME TO checkpoints")
        db.execute("INSERT INTO threads VALUES ('thread-1')")
        db.execute(
            "INSERT INTO checkpoints "
            "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,status,"
            "source_harness,source_model,writer_harness,writer_model,continuity_base_revision,"
            "continuity_base_snapshot_json,started_at) "
            "VALUES ('old-row','thread-1',0,1,'m-1','manual','cold','installed',"
            "'native','test','native','test',0,'[]','t0')"
        )
        db.commit()

        MIGRATIONS[37](db)

        assert db.execute("SELECT id, trigger FROM checkpoints").fetchall() == [
            ("old-row", "manual")
        ]
        db.execute(
            "INSERT INTO checkpoints "
            "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,status,"
            "source_harness,source_model,writer_harness,writer_model,continuity_base_revision,"
            "continuity_base_snapshot_json,started_at) "
            "VALUES ('agent-row','thread-1',0,2,'m-2','agent','cold','installed',"
            "'native','test','native','test',0,'[]','t1')"
        )
        assert db.execute(
            "SELECT trigger FROM checkpoints WHERE id='agent-row'"
        ).fetchone() == ("agent",)
        triggers = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert {
            "checkpoints_fts_insert",
            "checkpoints_fts_delete",
            "checkpoints_fts_update",
        } <= triggers
    finally:
        db.close()
