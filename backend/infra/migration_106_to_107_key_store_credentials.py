"""Schema 106 -> 107: move cloud-provider, web-search and webhook keys into the key store.

Before this step those keys lived in the settings table (``providers.<name>``,
``tools.web.search``, ``webhook``) and, for three of them, the backend's
environment. After it they live only in the key store file, and nothing reads
the old homes.

The step also gives ``key_store_entries`` its ``value_ref`` column. The store
on ``dev`` kept each value in the file under its entry name; each such value
whose entry has a row moves under a fresh reference the row names, and the
sweep after the commit drops the old flat names.

Each value is written under a fresh reference and the row naming it is written
in the runner's transaction, so the commit is the only commit point. A failure
rolls the transaction back and leaves every old copy in place; what the step
wrote to the file is unreferenced, and the next sweep removes it.

Where two copies of one key exist, a value already in the store wins, then the
settings value, then the environment. Only counts per kind are logged.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3

from infra.migration_secure_delete import secure_delete
from infra.time import utc_now_iso

log = logging.getLogger(__name__)

# Copied, not imported, so this step keeps producing the shape it was written for.
_PROVIDER_NAMES = frozenset({"anthropic", "openai", "openrouter", "deepseek", "gemini", "groq"})
_NETWORK_PLACEHOLDER_PROVIDER = "omlx"
_VALUE_PATTERN = re.compile(r"^[\x21-\x7e]{1,4096}$")
_ENV_VARS = {
    "provider-anthropic": "ANTHROPIC_API_KEY",
    "provider-openai": "OPENAI_API_KEY",
    "web-search": "BRAVE_API_KEY",
}


def _read_setting(conn: sqlite3.Connection, key: str) -> dict | None:
    # Ladder tests start from bare precursor schemas with no settings table; a real DB always has one.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'").fetchone() is None:
        return None
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        parsed = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _write_setting(conn: sqlite3.Connection, key: str, value: dict) -> None:
    conn.execute(
        "UPDATE settings SET value = ?, updated_at = ? WHERE key = ?",
        (json.dumps(value), utc_now_iso(), key),
    )


def _clean(value: object) -> str | None:
    """The usable text of a stored value, or None when blank or not storable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _VALUE_PATTERN.match(text) else None


def _collect(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, str], dict[str, dict], dict[str, int]]:
    """Settings and environment values by entry name, the cleaned settings, and unstorable counts by kind."""
    from_settings: dict[str, str] = {}
    left: dict[str, int] = {}
    cleaned: dict[str, dict] = {}

    providers = _read_setting(conn, "providers")
    if providers is not None:
        changed = False
        for name, cfg in providers.items():
            if not isinstance(cfg, dict) or "apiKey" not in cfg:
                continue
            if name == _NETWORK_PLACEHOLDER_PROVIDER:
                del cfg["apiKey"]
                changed = True
                continue
            if name not in _PROVIDER_NAMES:
                continue
            raw = cfg["apiKey"]
            value = _clean(raw)
            if value is not None:
                from_settings[f"provider-{name}"] = value
            elif isinstance(raw, str) and not raw.strip():
                pass  # a blanked key: nothing to move, the field just goes
            else:
                left["providers"] = left.get("providers", 0) + 1
                continue  # not storable: left where it is rather than lost
            del cfg["apiKey"]
            changed = True
        if changed:
            cleaned["providers"] = providers

    tools = _read_setting(conn, "tools")
    search = ((tools or {}).get("web") or {}).get("search")
    if tools is not None and isinstance(search, dict) and "apiKey" in search:
        value = _clean(search["apiKey"])
        if value is not None or not str(search["apiKey"] or "").strip():
            if value is not None:
                from_settings["web-search"] = value
            del search["apiKey"]
            cleaned["tools"] = tools
        else:
            left["web-search"] = 1

    webhook = _read_setting(conn, "webhook")
    if webhook is not None and "github_webhook_secret" in webhook:
        value = _clean(webhook["github_webhook_secret"])
        if value is not None or not str(webhook["github_webhook_secret"] or "").strip():
            if value is not None:
                from_settings["github-webhook"] = value
            del webhook["github_webhook_secret"]
            cleaned["webhook"] = webhook
        else:
            left["webhook"] = 1

    from_env = {}
    for entry, var in _ENV_VARS.items():
        value = _clean(os.environ.get(var, ""))
        if value is not None:
            from_env[entry] = value
        elif os.environ.get(var, "").strip():
            left["environment"] = left.get("environment", 0) + 1
    return from_settings, from_env, cleaned, left


def _add_value_ref(conn: sqlite3.Connection) -> bool:
    """Give the entries table its ``value_ref`` column; False when there is no table."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(key_store_entries)")}
    if not columns:
        return False
    if "value_ref" not in columns:
        conn.execute("ALTER TABLE key_store_entries ADD COLUMN value_ref TEXT")
    return True


def _convert_flat(conn: sqlite3.Connection) -> None:
    """Point each row at its flat-format value, written again under a fresh reference."""
    from infra import key_store_values

    flat = key_store_values.flat_values()
    for (name,) in conn.execute("SELECT name FROM key_store_entries WHERE value_ref IS NULL").fetchall():
        if flat.get(name):
            ref = key_store_values.put(conn, name, flat[name])
            conn.execute("UPDATE key_store_entries SET value_ref = ? WHERE name = ?", (ref, name))


def migrate_106_to_107(conn: sqlite3.Connection) -> None:
    """Move the keys into the store, then drop the settings fields."""
    from infra import key_store_values

    from_settings, from_env, cleaned, left = _collect(conn)
    if _add_value_ref(conn):
        _convert_flat(conn)
    now = utc_now_iso()
    moved_settings = key_store_values.store_missing(conn, from_settings, now)
    moved_environment = key_store_values.store_missing(
        conn, {name: value for name, value in from_env.items() if name not in from_settings}, now
    )

    with secure_delete(conn):
        for setting, value in cleaned.items():
            _write_setting(conn, setting, value)

    if left:
        log.warning(
            "key store migration: left %d value(s) in place because the key store cannot take them; by kind: %s",
            sum(left.values()),
            " ".join(f"{kind}={count}" for kind, count in sorted(left.items())),
        )
    moved = set(moved_settings) | set(moved_environment)
    log.info(
        "key store migration: moved %d key(s) from settings and %d from the environment; "
        "by kind: providers=%d web-search=%d webhook=%d",
        len(moved_settings),
        len(moved_environment),
        sum(1 for name in moved if name.startswith("provider-")),
        int("web-search" in moved),
        int("github-webhook" in moved),
    )
