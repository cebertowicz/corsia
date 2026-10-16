"""Durable identity, opaque configuration, and reach for connected apps."""

from __future__ import annotations

CONNECTED_APPS_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS connected_apps (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL CHECK(length(trim(name)) > 0),
        config_json TEXT NOT NULL CHECK(json_valid(config_json)),
        state TEXT NOT NULL DEFAULT 'disconnected' CHECK(state IN (
            'awaiting_approval', 'connecting', 'working', 'needs_token',
            'token_rejected', 'needs_sign_in', 'failed', 'disconnected'
        )),
        state_since TEXT NOT NULL,
        reach_mode TEXT NOT NULL CHECK(reach_mode IN ('all', 'selected')),
        outside_projects INTEGER NOT NULL DEFAULT 0
            CHECK(outside_projects IN (0, 1)),
        revision INTEGER NOT NULL DEFAULT 1
            CHECK(typeof(revision) = 'integer' AND revision > 0),
        created_by_thread_id TEXT,
        pending_config_json TEXT CHECK(
            pending_config_json IS NULL OR json_valid(pending_config_json)
        ),
        pending_previous_state TEXT,
        pending_actor_thread_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_connected_apps_name ON connected_apps(name);
    CREATE TABLE IF NOT EXISTS connected_app_reach (
        app_id TEXT NOT NULL REFERENCES connected_apps(id) ON DELETE CASCADE,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        PRIMARY KEY(app_id, project_id)
    );
    CREATE INDEX IF NOT EXISTS idx_connected_app_reach_project
        ON connected_app_reach(project_id, app_id);
    CREATE TABLE IF NOT EXISTS connected_app_audit (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        actor_kind TEXT NOT NULL CHECK(actor_kind IN ('person', 'agent', 'system')),
        actor_id TEXT NOT NULL,
        app_id TEXT NOT NULL,
        action TEXT NOT NULL,
        reach_from_json TEXT CHECK(reach_from_json IS NULL OR json_valid(reach_from_json)),
        reach_to_json TEXT CHECK(reach_to_json IS NULL OR json_valid(reach_to_json))
    );
    CREATE INDEX IF NOT EXISTS idx_connected_app_audit_app
        ON connected_app_audit(app_id, id);
"""
