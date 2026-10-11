"""Move credential values out of MCP server config and into the key store.

Before this step ``tools.mcpServers.<server>.env`` and ``.headers`` held every
value inline. After it, a credential-named value lives only in the key store
under ``mcp-<server>-<var>-<hash>`` and the config lists the variable in
``secret_env`` or ``secret_headers`` (see ``tools.mcp_secrets``, which owns the
naming and the credential rule). Non-credential values stay inline untouched.

Each value is written under a fresh reference and the row naming it is written
in the runner's transaction, so the commit is the only commit point. A failure
rolls the transaction back and leaves the inline copies in place; what the step
wrote to the file is unreferenced, and the next sweep removes it. A server
holding a value the store cannot take is left whole, not half moved.

Only counts are logged. Runs inside the runner's transaction; it never commits
or rolls back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from typing import Any

from infra.migration_secure_delete import secure_delete
from infra.time import utc_now_iso

log = logging.getLogger(__name__)

#: The schema version this step upgrades from. The one place the number lives,
#: so renumbering it behind another step is a single edit.
FROM_VERSION = 108

# Copied, not imported, so this step keeps judging storable values as it was written to.
_VALUE_PATTERN = re.compile(r"^[\x21-\x7e]{1,4096}$")
_MIGRATION_KINDS = {"secret_env": "env", "secret_headers": "headers"}
_MIGRATION_REFUSED_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
_MIGRATION_CREDENTIAL_KEY = (
    r"(?:pgpassword|(?:[A-Za-z0-9]+[-_])*(?:api[-_]?key|token|secret|password|passwd|credentials?|"
    r"private[-_]?key|secret[-_]access[-_]key))"
)
_MIGRATION_CREDENTIAL_RE = re.compile(
    rf"^(?:{_MIGRATION_CREDENTIAL_KEY}|authorization|cookie)$", re.IGNORECASE,
)


def _migration_entry_name(server: str, field: str, var: str) -> str:
    kind = _MIGRATION_KINDS.get(field, field)
    digest = hashlib.sha256(f"{kind}\0{server}\0{var}".encode()).hexdigest()[:10]
    slug = lambda text, limit: re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "x"
    return f"mcp-{slug(server, 10)}-{slug(var, 14)}-{digest}"


def _migration_listed(config: dict, field: str) -> list[str]:
    from tools.mcp_config import MCPConfigError

    names = config.get(field, [])
    if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
        raise MCPConfigError(f"{field} must be a list of variable names")
    return names


def _migration_literals(config: dict, source: str) -> dict:
    from tools.mcp_config import MCPConfigError

    values = config.get(source) or {}
    if not isinstance(values, dict):
        raise MCPConfigError(f"{source} must be an object of string values")
    return values


def _migration_split_secrets(server: str, config: dict) -> tuple[dict, dict[str, str]]:
    from tools.mcp_config import MCPConfigError

    for field, source in (("secret_env", "env"), ("secret_headers", "headers")):
        names = _migration_listed(config, field)
        if len(names) != len(set(names)):
            raise MCPConfigError(f"{field} lists a name twice")
        if set(names) & set(_migration_literals(config, source)):
            raise MCPConfigError(f"{field} and {source} both set the same name")
    if any(name.lower() in _MIGRATION_REFUSED_HEADERS for name in _migration_listed(config, "secret_headers")):
        raise MCPConfigError("literal credential headers are refused; use bearer_file for bearer authentication")

    result = dict(config)
    lifted: dict[str, str] = {}
    for field, source in (("secret_env", "env"), ("secret_headers", "headers")):
        values = _migration_literals(config, source)
        keep: dict[str, Any] = {}
        names = list(_migration_listed(config, field))
        for var, value in values.items():
            credential = isinstance(var, str) and bool(
                _MIGRATION_CREDENTIAL_RE.fullmatch(var.strip().strip("\"'").casefold())
            )
            movable = isinstance(value, str) and value.strip() and credential and not (
                source == "headers" and var.lower() in _MIGRATION_REFUSED_HEADERS
            )
            if movable:
                lifted[_migration_entry_name(server, field, var)] = value
                names.append(var)
            else:
                keep[var] = value
        if values:
            result[source] = keep
            if not keep:
                del result[source]
        if names:
            result[field] = names
    return result, lifted


def _read_tools(conn: sqlite3.Connection) -> dict | None:
    # Ladder tests start from bare precursor schemas with no settings table; a real DB always has one.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'").fetchone() is None:
        return None
    row = conn.execute("SELECT value FROM settings WHERE key = 'tools'").fetchone()
    if row is None:
        return None
    try:
        parsed = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def migrate_mcp_secrets(conn: sqlite3.Connection) -> None:
    """Move each server's credential values into the store, then drop them from config."""
    from infra import key_store_values
    from tools.mcp_config import MCPConfigError

    tools = _read_tools(conn)
    if tools is None or not isinstance(tools.get("mcpServers"), dict):
        return
    servers = tools["mcpServers"]

    cleaned: dict[str, dict] = {}
    lifted: dict[str, str] = {}
    skipped = 0
    for server, config in servers.items():
        if not isinstance(config, dict):
            continue
        try:
            clean, values = _migration_split_secrets(server, config)
        except MCPConfigError:
            skipped += 1  # malformed: left as it is rather than lost
            continue
        if not values:
            continue
        if any(not _VALUE_PATTERN.match(value.strip()) for value in values.values()):
            skipped += 1
            continue
        cleaned[server] = clean
        lifted.update({name: value.strip() for name, value in values.items()})

    if cleaned:
        now = utc_now_iso()
        moved = key_store_values.store_missing(conn, lifted, now)
        tools["mcpServers"] = {**servers, **cleaned}
        with secure_delete(conn):
            conn.execute(
                "UPDATE settings SET value = ?, updated_at = ? WHERE key = 'tools'",
                (json.dumps(tools), now),
            )
        log.info(
            "mcp secrets migration: moved %d credential value(s) from %d server(s) into the key store",
            len(moved), len(cleaned),
        )
    if skipped:
        log.warning("mcp secrets migration: left %d server(s) unchanged because a value could not be stored", skipped)
