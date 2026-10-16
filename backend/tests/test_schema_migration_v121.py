"""Schema 120 -> 121: stable connected-app identity and explicit reach."""

from __future__ import annotations

import sqlite3

from infra.database_migrations import MIGRATIONS
from infra.migration_120_to_121_connected_apps import migrate_120_to_121
from infra.schema import SCHEMA_VERSION


def test_v121_registers_connected_apps_step() -> None:
    assert SCHEMA_VERSION == 121
    assert MIGRATIONS[120] is migrate_120_to_121


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
        migrate_120_to_121(db)
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
