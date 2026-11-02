"""Schema v15 archives every active Codex thread handle once, and nothing else.

A resumed Codex provider thread keeps the tools it was started with, so after a
tool rename it can never reach the renamed tool. The migration archives each
active ``codex``/``thread`` identity the way ``registry.models.retire`` does, so
the thread's next Turn starts a fresh provider thread with the current tools.
"""

from __future__ import annotations

import json
import sqlite3

_ROLLOUT = {"rollout_path": "/rollouts/codex-live.jsonl", "reasoning_effort": "high"}


def _identity(db, artifact_id, integration, type_, external_id, state, metadata=None):
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,state,metadata,created_at,updated_at)
           VALUES (?,?,?,?,?,?,'t0','t0')""",
        (artifact_id, integration, type_, external_id, state,
         json.dumps(metadata) if metadata is not None else None),
    )


def _registration(db, registration_id, artifact_id, thread_id, relationship_state):
    db.execute(
        """INSERT INTO artifact_registrations
           (id,artifact_id,parent_thread_id,relationship_state,created_at,updated_at)
           VALUES (?,?,?,?,'t0','t0')""",
        (registration_id, artifact_id, thread_id, relationship_state),
    )


def _schema_fourteen_database() -> sqlite3.Connection:
    """A v14 database with live and settled handles across integrations."""
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute("PRAGMA user_version=14")
    for thread_id in ("codex-worker", "watcher", "cc-worker"):
        db.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) "
            "VALUES (?,'user-1','t0','t0')",
            (thread_id,),
        )
    _identity(db, "live-codex", "codex", "thread", "provider-live", "active", _ROLLOUT)
    _registration(db, "live-owner", "live-codex", "codex-worker", "owner-active")
    _registration(db, "live-watcher", "live-codex", "watcher", "subscriber-recent")
    _identity(db, "old-codex", "codex", "thread", "provider-old", "archived", _ROLLOUT)
    _registration(db, "old-owner", "old-codex", "codex-worker", "subscriber-archived")
    _identity(db, "cc-session", "claude-code", "session", "cc-live", "active")
    _registration(db, "cc-owner", "cc-session", "cc-worker", "owner-active")
    _identity(db, "codex-file", "codex", "file", "notes.md", "active")
    _registration(db, "file-owner", "codex-file", "codex-worker", "owner-active")
    db.commit()
    return db


def _identities(db) -> dict[str, tuple]:
    return {
        row["id"]: (row["state"], row["updated_at"], row["metadata"])
        for row in db.execute("SELECT id,state,updated_at,metadata FROM artifact_identities")
    }


def _registrations(db) -> dict[str, tuple]:
    return {
        row["id"]: (row["relationship_state"], row["updated_at"])
        for row in db.execute(
            "SELECT id,relationship_state,updated_at FROM artifact_registrations"
        )
    }


def test_fresh_schema_is_current_version() -> None:
    from infra.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == 169


def test_schema_fourteen_archives_each_active_codex_thread_handle_and_nothing_else() -> None:
    from infra.database_migrations import migrate_to_current

    db = _schema_fourteen_database()
    try:
        identities_before = _identities(db)
        registrations_before = _registrations(db)

        result = migrate_to_current(db, 15)

        assert (result.migrated, result.from_version, result.to_version) == (True, 14, 15)
        identities = _identities(db)
        registrations = _registrations(db)
        state, updated_at, metadata = identities["live-codex"]
        assert state == "archived"
        assert updated_at != "t0"
        assert json.loads(metadata) == _ROLLOUT
        assert registrations["live-owner"] == registrations_before["live-owner"]
        assert registrations["live-watcher"][0] == "subscriber-archived"
        for artifact_id in ("old-codex", "cc-session", "codex-file"):
            assert identities[artifact_id] == identities_before[artifact_id]
        for registration_id in ("old-owner", "cc-owner", "file-owner"):
            assert registrations[registration_id] == registrations_before[registration_id]
    finally:
        db.close()


def test_the_archived_handle_is_no_longer_found_for_resume() -> None:
    from infra.database_migrations import migrate_to_current
    from subagents.codex_runtime_support import codex_thread_registration

    db = _schema_fourteen_database()
    try:
        assert codex_thread_registration("codex-worker", db)[0] == "provider-live"

        migrate_to_current(db, 15)

        assert codex_thread_registration("codex-worker", db) == (None, None, None)
    finally:
        db.close()


def test_the_migration_runs_once() -> None:
    from infra.database_migrations import migrate_to_current

    db = _schema_fourteen_database()
    try:
        migrate_to_current(db, 15)
        identities = _identities(db)
        registrations = _registrations(db)

        repeated = migrate_to_current(db, 15)

        assert (repeated.migrated, repeated.from_version) == (False, 15)
        assert _identities(db) == identities
        assert _registrations(db) == registrations
    finally:
        db.close()
