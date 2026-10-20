"""Schema v118 renames the workspace conductor's persisted names to CORSIA agent."""

from __future__ import annotations

import json
import sqlite3

from infra.database_migrations import MIGRATIONS
from infra.migration_117_to_118_corsia_agent import migrate_117_to_118
from infra.schema import SCHEMA_VERSION

OLD_KEY = "workspace_conductor_session_id"
NEW_KEY = "corsia_agent_thread_id"


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, project_id TEXT);
        CREATE TABLE automation_schedules (id TEXT PRIMARY KEY, payload_json TEXT);
        CREATE TABLE context_change_journal (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            object_kind TEXT NOT NULL,
            object_id TEXT NOT NULL,
            summary TEXT
        );
        """
    )
    return db


def _setting(db: sqlite3.Connection, key: str, thread_id: str) -> None:
    db.execute(
        "INSERT INTO settings (key,value,updated_at) VALUES (?,?,'t')",
        (key, json.dumps({"session_id": thread_id})),
    )


def _keys(db: sqlite3.Connection) -> dict[str, str]:
    return {
        row["key"]: json.loads(row["value"])["session_id"]
        for row in db.execute("SELECT key,value FROM settings")
    }


def test_registered_as_the_current_step():
    assert SCHEMA_VERSION == 130
    assert MIGRATIONS[117] is migrate_117_to_118


def test_seeded_old_payloads_are_renamed_in_one_pass():
    db = _db()
    _setting(db, OLD_KEY, "ws")
    db.executemany(
        "INSERT INTO threads (id,title,project_id) VALUES (?,?,?)",
        [
            ("ws", "Conductor", None),
            ("proj", "Conductor", "p1"),
            ("custom", "Mine", None),
        ],
    )
    db.executemany(
        "INSERT INTO automation_schedules (id,payload_json) VALUES (?,?)",
        [
            ("s1", json.dumps({"kind": "conductor.health_check"})),
            ("s2", json.dumps({"kind": "other"})),
            ("s3", None),
        ],
    )
    db.executemany(
        "INSERT INTO context_change_journal (object_kind,object_id,summary) VALUES (?,?,?)",
        [
            ("agent_roster", "user:u1:workspace-conductor-contact", "a"),
            ("agent_roster", "user:u1:workspace", "b"),
            ("thread_context", "user:u1:workspace-conductor-contact", "c"),
        ],
    )

    migrate_117_to_118(db)

    assert _keys(db) == {NEW_KEY: "ws"}
    titles = {r["id"]: r["title"] for r in db.execute("SELECT id,title FROM threads")}
    assert titles == {"ws": "CORSIA", "proj": "Conductor", "custom": "Mine"}
    payloads = {
        r["id"]: r["payload_json"]
        for r in db.execute("SELECT id,payload_json FROM automation_schedules")
    }
    assert json.loads(payloads["s1"]) == {"kind": "corsia_agent.health_check"}
    assert json.loads(payloads["s2"]) == {"kind": "other"}
    assert payloads["s3"] is None
    ids = {
        r["summary"]: r["object_id"]
        for r in db.execute("SELECT summary,object_id FROM context_change_journal")
    }
    assert ids == {
        "a": "user:u1:corsia-agent-contact",
        "b": "user:u1:workspace",
        "c": "user:u1:workspace-conductor-contact",
    }


def test_idempotent():
    db = _db()
    _setting(db, OLD_KEY, "ws")
    db.execute("INSERT INTO threads (id,title) VALUES ('ws','Conductor')")
    migrate_117_to_118(db)
    migrate_117_to_118(db)
    assert _keys(db) == {NEW_KEY: "ws"}


def test_new_key_only_is_untouched_and_its_title_renamed():
    db = _db()
    _setting(db, NEW_KEY, "ws")
    db.execute("INSERT INTO threads (id,title) VALUES ('ws','Conductor')")
    migrate_117_to_118(db)
    assert _keys(db) == {NEW_KEY: "ws"}
    assert db.execute("SELECT title FROM threads").fetchone()[0] == "CORSIA"


def test_both_keys_keep_new_and_delete_old():
    db = _db()
    _setting(db, OLD_KEY, "old-thread")
    _setting(db, NEW_KEY, "new-thread")
    db.executemany(
        "INSERT INTO threads (id,title) VALUES (?,?)",
        [("old-thread", "Conductor"), ("new-thread", "Conductor")],
    )
    migrate_117_to_118(db)
    assert _keys(db) == {NEW_KEY: "new-thread"}
    titles = {r["id"]: r["title"] for r in db.execute("SELECT id,title FROM threads")}
    assert titles == {"old-thread": "Conductor", "new-thread": "CORSIA"}


def test_no_designation_changes_no_titles():
    db = _db()
    db.execute("INSERT INTO threads (id,title) VALUES ('t','Conductor')")
    migrate_117_to_118(db)
    assert db.execute("SELECT title FROM threads").fetchone()[0] == "Conductor"


def test_runner_upgrades_a_117_database_in_one_transaction(tmp_path):
    from infra.database_migrations import migrate_to_current

    path = tmp_path / "upgrade.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT);
            INSERT INTO threads VALUES ('ws', 'Conductor');
            PRAGMA user_version=117;
            """
        )
        _setting(connection, OLD_KEY, "ws")
    with sqlite3.connect(path) as connection:
        result = migrate_to_current(connection, SCHEMA_VERSION)
        rows = connection.execute("SELECT key, value FROM settings").fetchall()
        title = connection.execute("SELECT title FROM threads").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert result.migrated is True
    assert (result.from_version, result.to_version) == (117, 130)
    assert version == 130
    assert [(k, json.loads(v)["session_id"]) for k, v in rows] == [(NEW_KEY, "ws")]
    assert title == "CORSIA"
