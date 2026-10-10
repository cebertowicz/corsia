"""DB-backed settings helpers."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

from infra.database import get_db, write_transaction

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_setting(key: str, *, db: sqlite3.Connection | None = None) -> dict | None:
    """Read object-valued JSON, optionally on the caller's named connection."""
    db = db if db is not None else get_db()
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    try:
        parsed = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def set_setting(key: str, value: dict, *, db: sqlite3.Connection | None = None) -> None:
    """Upsert JSON in the named caller's transaction or a standalone owner."""
    with write_transaction(db, scope="set_setting") as connection:
        connection.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value), _now()),
        )


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base, recursing into dicts."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def get_api_keys() -> dict[str, str]:
    """Extract API keys from providers config."""
    providers = get_setting("providers") or {}
    keys = {}
    for name, prov in providers.items():
        if isinstance(prov, dict):
            if api_key := prov.get("apiKey"):
                keys[name] = api_key
    return keys


def get_agent_defaults() -> dict:
    """Get agent default settings."""
    agents = get_setting("agents") or {}
    return agents.get("defaults", {})


def get_host_access_auto_approve() -> bool:
    """Return whether reviewer-approved host-access leases activate automatically."""
    return get_agent_defaults().get("host_access_auto_approve") is True


def get_host_access_reviewer_model(*, db=None) -> str | None:
    """Return the exact configured catalog ID for host-access review."""
    return _exact_agent_default("host_access_reviewer_model", db=db)


def _exact_agent_default(key: str, *, db=None) -> str | None:
    if db is None:
        defaults = get_agent_defaults()
    else:
        row = db.execute(
            "SELECT value FROM settings WHERE key='agents'"
        ).fetchone()
        try:
            agents = json.loads(row["value"]) if row else {}
        except (json.JSONDecodeError, TypeError):
            agents = {}
        defaults = agents.get("defaults", {}) if isinstance(agents, dict) else {}
        if not isinstance(defaults, dict):
            defaults = {}
    configured = defaults.get(key)
    if not isinstance(configured, str) or not configured.strip():
        return None
    return configured.strip()


def get_provider_config(name: str) -> dict:
    """Get config for a specific provider."""
    providers = get_setting("providers") or {}
    return providers.get(name, {})


def get_providers() -> dict:
    """Get all providers config."""
    return get_setting("providers") or {}


def get_tool_config() -> dict:
    """Get tool configuration."""
    return get_setting("tools") or {}


def get_mcp_servers() -> dict:
    """Get MCP server configuration."""
    return get_tool_config().get("mcpServers", {})


def get_feature_flags() -> dict:
    """Get feature flags. Defaults to True when not explicitly configured."""
    return get_setting("features") or {}


def save_setting_merge(key: str, updates: dict) -> None:
    """Deep-merge updates into an existing setting."""
    with write_transaction(None, scope="save_setting_merge") as db:
        current = get_setting(key, db=db) or {}
        set_setting(key, _deep_merge(current, updates), db=db)


def save_agent_defaults(updates: dict) -> None:
    """Merge updates into agents.defaults."""
    save_setting_merge("agents", {"defaults": updates})


def delete_agent_default(key: str) -> None:
    """Remove one agent default rather than persisting a null override."""
    with write_transaction(None, scope="delete_agent_default") as db:
        agents = get_setting("agents", db=db) or {}
        defaults = agents.get("defaults") if isinstance(agents, dict) else None
        if not isinstance(defaults, dict) or key not in defaults:
            return
        defaults = dict(defaults)
        defaults.pop(key)
        updated = dict(agents)
        updated["defaults"] = defaults
        set_setting("agents", updated, db=db)


def bootstrap_env() -> None:
    """Set environment variables from DB settings (API keys, etc.)."""
    keys = get_api_keys()
    env_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
    }
    for provider_name, env_var in env_map.items():
        if provider_name in keys and env_var not in os.environ:
            os.environ[env_var] = keys[provider_name]
