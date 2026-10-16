"""Schema 120 -> 121: move MCP apps from mutable setting names to stable ids."""

from __future__ import annotations

import json
import sqlite3
import uuid

from infra.connected_apps_schema import CONNECTED_APPS_SCHEMA_SQL
from infra.time import utc_now_iso
from tools.mcp_secrets import entry_name, referenced_entries


def migrate_120_to_121(conn: sqlite3.Connection) -> None:
    """Move each configured server and its credential references in the caller's transaction.

    The old settings key and old credential entry names disappear together. The
    key-store value file is never read: the value_ref is copied unchanged.
    """
    conn.executescript(CONNECTED_APPS_SCHEMA_SQL)
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
    ).fetchone() is None:
        return
    row = conn.execute("SELECT value FROM settings WHERE key='tools'").fetchone()
    if row is None:
        return
    try:
        tools = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(tools, dict):
        return
    servers = tools.get("mcpServers")
    if servers is None:
        return
    if not isinstance(servers, dict):
        raise ValueError("mcpServers must be an object")

    now = utc_now_iso()
    for name, config in servers.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(config, dict):
            raise ValueError("configured MCP servers must have a name and object config")
        app_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO connected_apps("
            "id,name,config_json,state,state_since,reach_mode,outside_projects,created_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?)",
            (app_id, name, json.dumps(config, separators=(",", ":"), sort_keys=True),
             "disconnected", now, "all", 0, now, now),
        )
        _rekey_references(conn, old_name=name, app_id=app_id, config=config)

    updated = dict(tools)
    updated.pop("mcpServers")
    conn.execute(
        "UPDATE settings SET value=? WHERE key='tools'",
        (json.dumps(updated, separators=(",", ":"), sort_keys=True),),
    )


def _rekey_references(
    conn: sqlite3.Connection, *, old_name: str, app_id: str, config: dict
) -> None:
    """Rename managed MCP entries while retaining their opaque value references."""
    referenced_entries(old_name, config)  # validate the stored reference shape first
    pairs = [
        (entry_name(old_name, field, variable), entry_name(app_id, field, variable))
        for field in ("secret_env", "secret_headers")
        for variable in config.get(field, [])
    ]
    old_entries = {old_entry for old_entry, _ in pairs}
    if len(old_entries) != len(pairs) or len({new_entry for _, new_entry in pairs}) != len(pairs):
        raise ValueError("configured MCP credential references are invalid")
    for old_entry, new_entry in pairs:
        row = conn.execute(
            "SELECT label,description,added_by_thread_id,added_by_name,created_at,set_at,value_ref "
            "FROM key_store_entries WHERE name=?",
            (old_entry,),
        ).fetchone()
        if row is None:
            continue
        conn.execute(
            "INSERT INTO key_store_entries("
            "name,label,description,added_by_thread_id,added_by_name,created_at,set_at,value_ref"
            ") VALUES (?,?,?,?,?,?,?,?)",
            (new_entry, *tuple(row)),
        )
    for old_entry in old_entries:
        conn.execute("DELETE FROM key_store_entries WHERE name=?", (old_entry,))
