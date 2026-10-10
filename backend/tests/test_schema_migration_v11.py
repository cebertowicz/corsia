"""Startup migration 10 -> 11 proof for artifact-pane clean slate."""

from __future__ import annotations

import sqlite3

from infra.database_migrations import migrate_to_current


def _legacy_db(path, *, version: int) -> None:
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
        CREATE TABLE artifact_identities (
            id TEXT PRIMARY KEY,
            integration TEXT NOT NULL,
            type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT,
            description TEXT,
            path TEXT,
            state TEXT NOT NULL DEFAULT 'active',
            retention TEXT NOT NULL DEFAULT 'durable',
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(integration,type,external_id)
        );
        CREATE TABLE artifact_registrations (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
            parent_thread_id TEXT NOT NULL,
            relationship_state TEXT NOT NULL DEFAULT 'owner-active',
            tmp INTEGER NOT NULL DEFAULT 0,
            surfacing TEXT NOT NULL DEFAULT 'visible'
                CHECK (surfacing IN ('visible','quiet')),
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(artifact_id,parent_thread_id)
        );
        CREATE TABLE artifact_registration_events (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
            event_type TEXT NOT NULL,
            actor_thread_id TEXT,
            authority TEXT,
            reason TEXT,
            source_path TEXT,
            causal_event_id TEXT,
            affected_thread_id TEXT,
            old_owner_thread_id TEXT,
            new_owner_thread_id TEXT,
            old_relationship_state TEXT,
            new_relationship_state TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL
        );
        INSERT INTO threads VALUES ('thread-1');
        INSERT INTO checkpoints VALUES ('cp-1','thread-1',0,'m1','idle','installed');
        INSERT INTO active_turns
            (id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch)
            VALUES ('turn-1','thread-1',0,'native/test','completed','t0','t1','boot');
        INSERT INTO artifact_identities
            (id,integration,type,external_id,state,retention,created_at,updated_at) VALUES
            ('visible','workspace','document','visible.md','active','durable','t0','t0'),
            ('quiet','workspace','document','quiet.md','active','durable','t0','t0'),
            ('pinned','workspace','document','pinned.md','active','durable','t0','t0'),
            ('archived','workspace','document','archived.md','archived','durable','t0','t0'),
            ('session','claude-code','session','cc-1','active','durable','t0','t0'),
            ('codex','codex','thread','cx-1','active','durable','t0','t0');
        INSERT INTO artifact_registrations
            (id,artifact_id,parent_thread_id,relationship_state,surfacing,created_at,updated_at) VALUES
            ('r-visible','visible','thread-1','owner-active','visible','t0','t0'),
            ('r-quiet','quiet','thread-1','owner-active','quiet','t0','t0'),
            ('r-pinned','pinned','thread-1','owner-pinned','visible','t0','t0'),
            ('r-archived','archived','thread-1','owner-active','visible','t0','t0'),
            ('r-session','session','thread-1','owner-active','visible','t0','t0'),
            ('r-codex','codex','thread-1','subscriber-recent','visible','t0','t0');
        """
    )
    if version == 10:
        db.execute(
            "ALTER TABLE active_turns ADD COLUMN idle_checkpoint_pending "
            "INTEGER NOT NULL DEFAULT 0"
        )
    db.execute(f"PRAGMA user_version={version}")
    db.commit()
    db.close()


def _assert_artifact_cutover(db) -> None:
    columns = {
        row[1] for row in db.execute("PRAGMA table_info(artifact_registrations)")
    }
    assert {"origin_plan_node_id", "node_retired_at"} <= columns
    rows = {
        row[0]: tuple(row[1:])
        for row in db.execute(
            """SELECT artifact_id,surfacing,origin_plan_node_id,node_retired_at
               FROM artifact_registrations ORDER BY artifact_id"""
        )
    }
    assert rows["visible"][0] == "settled"
    assert rows["visible"][1] is None and rows["visible"][2] is not None
    assert rows["quiet"] == ("quiet", None, None)
    assert rows["pinned"] == ("visible", None, None)
    assert rows["archived"] == ("visible", None, None)
    assert rows["session"] == ("visible", None, None)
    assert rows["codex"] == ("visible", None, None)


def test_v10_database_migrates_to_v11_with_clean_slate(tmp_path):
    path = tmp_path / "v10.db"
    _legacy_db(path, version=10)
    db = sqlite3.connect(path)
    try:
        result = migrate_to_current(db, 11)
        assert result.migrated
        assert (result.from_version, result.to_version) == (10, 11)
        _assert_artifact_cutover(db)
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_v9_database_runs_checkpoint_then_artifact_steps_to_v11(tmp_path):
    path = tmp_path / "v9.db"
    _legacy_db(path, version=9)
    db = sqlite3.connect(path)
    try:
        result = migrate_to_current(db, 11)
        assert result.migrated
        assert (result.from_version, result.to_version) == (9, 11)
        _assert_artifact_cutover(db)
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 0
        assert db.execute(
            """SELECT 1 FROM sqlite_master WHERE type='index'
               AND name='idx_active_turns_idle_checkpoint_due'"""
        ).fetchone() is not None
        assert db.execute(
            """SELECT 1 FROM sqlite_master WHERE type='index'
               AND name='checkpoints_idle_boundary'"""
        ).fetchone() is not None
    finally:
        db.close()


def test_schema_11_version_gate_does_not_repeat_clean_slate(tmp_path):
    path = tmp_path / "v11.db"
    _legacy_db(path, version=10)
    db = sqlite3.connect(path)
    try:
        migrate_to_current(db, 11)
        db.execute(
            """INSERT INTO artifact_identities
               (id,integration,type,external_id,state,retention,created_at,updated_at)
               VALUES ('new','workspace','document','new.md','active','durable','t1','t1')"""
        )
        db.execute(
            """INSERT INTO artifact_registrations
               (id,artifact_id,parent_thread_id,relationship_state,surfacing,
                created_at,updated_at)
               VALUES ('r-new','new','thread-1','owner-active','visible','t1','t1')"""
        )
        db.commit()
        result = migrate_to_current(db, 11)
        assert not result.migrated
        assert db.execute(
            "SELECT surfacing FROM artifact_registrations WHERE id='r-new'"
        ).fetchone()[0] == "visible"
    finally:
        db.close()
