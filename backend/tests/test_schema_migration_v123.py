"""Schema 122 -> 123: stable connected-app identity and explicit reach."""

from __future__ import annotations

import json
import sqlite3

from connections.app_reach import key_ref_descriptors, runtime_app_config
from infra import key_store
from infra.database_migrations import MIGRATIONS
from infra.migration_122_to_123_connected_apps import migrate_122_to_123
from infra.schema import SCHEMA_VERSION
from tools import mcp_secrets
from tools.mcp_secrets import entry_name


def test_v122_registers_connected_apps_step() -> None:
    assert SCHEMA_VERSION == 126
    assert MIGRATIONS[122] is migrate_122_to_123


def test_migration_rekeys_a_set_legacy_key_for_runtime_resolution_and_descriptors(monkeypatch) -> None:
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE key_store_entries (
                name TEXT PRIMARY KEY, label TEXT, description TEXT, added_by_thread_id TEXT,
                added_by_name TEXT, created_at TEXT NOT NULL, set_at TEXT, value_ref TEXT
            );
            """
        )
        legacy = {"command": "fixture", "secret_env": ["SERVICE_TOKEN"]}
        db.execute("INSERT INTO settings VALUES ('tools', ?, 't')", (json.dumps({"mcpServers": {"Legacy": legacy}}),))
        db.execute(
            "INSERT INTO key_store_entries VALUES (?,?,?,?,?,?,?,?)",
            (entry_name("Legacy", "secret_env", "SERVICE_TOKEN"), "Legacy key", "", None, None, "t", "t", "opaque-ref"),
        )
        migrate_122_to_123(db)
        app_id = db.execute("SELECT id FROM connected_apps WHERE name='Legacy'").fetchone()["id"]

        name, runtime = runtime_app_config(app_id, db=db)
        monkeypatch.setattr(
            key_store, "resolve_names", lambda names, db=None: {entry: "migrated-value" for entry in names},
        )

        assert name == "Legacy"
        assert runtime["secret_env"] == ["SERVICE_TOKEN"]
        assert mcp_secrets.resolve(app_id, runtime, db=db) == ({"SERVICE_TOKEN": "migrated-value"}, {})
        assert key_ref_descriptors(app_id, json.loads(db.execute(
            "SELECT config_json FROM connected_apps WHERE id=?", (app_id,)
        ).fetchone()["config_json"]), db=db) == [{"kind": "env", "name": "SERVICE_TOKEN", "set": True}]


def test_connected_apps_step_creates_identity_and_reach_tables() -> None:
    with sqlite3.connect(":memory:") as db:
        db.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE key_store_entries (
                name TEXT PRIMARY KEY, label TEXT, description TEXT, added_by_thread_id TEXT,
                added_by_name TEXT, created_at TEXT NOT NULL, set_at TEXT, value_ref TEXT
            );
            """
        )
        migrate_122_to_123(db)
        assert [row[1] for row in db.execute("PRAGMA table_info(connected_apps)")] == [
            "id", "name", "config_json", "state", "state_since", "reach_mode",
            "outside_projects", "revision", "created_by_thread_id", "pending_config_json",
            "pending_previous_state", "pending_actor_thread_id", "created_at", "updated_at",
        ]
        assert [row[1] for row in db.execute("PRAGMA table_info(connected_app_reach)")] == [
            "app_id", "project_id",
        ]
        assert [row[1] for row in db.execute("PRAGMA table_info(connected_app_audit)")] == [
            "id", "created_at", "actor_kind", "actor_id", "app_id", "action",
            "reach_from_json", "reach_to_json",
        ]
