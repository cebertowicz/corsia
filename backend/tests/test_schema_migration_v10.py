"""Startup migration 9 -> 10 proof for producer-owned idle due state."""

from __future__ import annotations

import sqlite3


def test_migration_preserves_duplicate_idle_boundaries_without_rearming_turns(
    tmp_path,
) -> None:
    from infra.database_migrations import migrate_to_current

    path = tmp_path / "corsia.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE threads (id TEXT PRIMARY KEY);
        CREATE TABLE checkpoints (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            history_epoch INTEGER NOT NULL,
            through_message_id TEXT NOT NULL,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE active_turns (
            turn_order INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL REFERENCES threads(id),
            execution_epoch INTEGER NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            error TEXT,
            boot_epoch TEXT NOT NULL,
            retry_at TEXT,
            plan_id TEXT,
            plan_node_id TEXT,
            plan_attempt_id TEXT,
            plan_commitment_id TEXT,
            plan_usage_role TEXT
        );
        INSERT INTO threads VALUES ('thread-1');
        INSERT INTO checkpoints
            VALUES ('cp-1','thread-1',0,'m1','idle','installed');
        INSERT INTO checkpoints
            VALUES ('cp-2','thread-1',0,'m1','idle','failed');
        INSERT INTO active_turns
            (id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch)
            VALUES ('turn-1','thread-1',0,'native/test','completed','t0','t1','boot');
        PRAGMA user_version=9;
        """
    )
    db.commit()
    db.close()

    db = sqlite3.connect(path)
    try:
        result = migrate_to_current(db, 10)
        assert result.migrated and (result.from_version, result.to_version) == (9, 10)
    finally:
        db.close()

    db = sqlite3.connect(path)
    try:
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 0
        for index in (
            "idx_active_turns_idle_checkpoint_due",
            "checkpoints_idle_boundary",
        ):
            assert db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                (index,),
            ).fetchone() is not None
        assert db.execute(
            "SELECT id FROM checkpoints ORDER BY id"
        ).fetchall() == [("cp-1",), ("cp-2",)]
        indexes = {
            str(row[1]): int(row[2])
            for row in db.execute("PRAGMA index_list(checkpoints)")
        }
        assert indexes["checkpoints_idle_boundary"] == 0
    finally:
        db.close()
