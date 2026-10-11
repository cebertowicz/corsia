"""MCP server credentials, kept in the key store and referenced from config.

A credential-named ``env`` or ``headers`` value never stays in the settings
table. Saving a server stores each such value under ``mcp-<server>-<var>-<hash>``
and leaves only the variable's name in the server's ``secret_env`` or
``secret_headers`` list. The entry name is derived, never read from config, so
a hand-edited config can name a variable but cannot point at any other key.

The launcher resolves the lists with :func:`resolve` just before it opens a
connection; the values live in that one call's result and nowhere else. Errors
here carry fixed text and never a value.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

from host_access.credential_sanitizer import is_credential_name
from infra import key_store
from infra.key_store_mcp import PREFIX as MCP_PREFIX
from tools.mcp_config import MCPConfigError

SECRET_ENV = "secret_env"
SECRET_HEADERS = "secret_headers"
_KINDS = {SECRET_ENV: "env", SECRET_HEADERS: "headers"}
#: Header names HTTP MCP config refuses outright, whether literal or stored.
REFUSED_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})

# 4 + 10 + 1 + 14 + 1 + 10 characters: the longest name still fits the store's 40.
_SERVER_SLUG = 10
_VAR_SLUG = 14
_HASH_LENGTH = 10


def _slug(text: str, limit: int) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "x"


def entry_name(server: str, field: str, var: str) -> str:
    """The store entry for ``var`` in ``server``'s ``field`` (``env`` or ``headers``).

    The readable parts are truncated to fit the name rule, so a short hash of
    the exact server, field and variable keeps two different ones from sharing
    an entry, however alike their slugs.
    """
    kind = _KINDS.get(field, field)
    digest = hashlib.sha256(f"{kind}\0{server}\0{var}".encode()).hexdigest()[:_HASH_LENGTH]
    name = f"{MCP_PREFIX}{_slug(server, _SERVER_SLUG)}-{_slug(var, _VAR_SLUG)}-{digest}"
    return name


def _listed(config: dict, field: str) -> list[str]:
    names = config.get(field, [])
    if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
        raise MCPConfigError(f"{field} must be a list of variable names")
    return names


def referenced_entries(server: str, config: dict) -> set[str]:
    """Every store entry ``config`` refers to."""
    return {
        entry_name(server, field, var)
        for field in _KINDS
        for var in _listed(config, field)
    }


def _literals(config: dict, source: str) -> dict:
    values = config.get(source) or {}
    if not isinstance(values, dict):
        raise MCPConfigError(f"{source} must be an object of string values")
    return values


def check_references(config: dict) -> None:
    """Refuse a reference list that is malformed or that duplicates a literal value."""
    for field, source in ((SECRET_ENV, "env"), (SECRET_HEADERS, "headers")):
        names = _listed(config, field)
        if len(names) != len(set(names)):
            raise MCPConfigError(f"{field} lists a name twice")
        if set(names) & set(_literals(config, source)):
            raise MCPConfigError(f"{field} and {source} both set the same name")
    if any(name.lower() in REFUSED_HEADERS for name in _listed(config, SECRET_HEADERS)):
        raise MCPConfigError("literal credential headers are refused; use bearer_file for bearer authentication")


def split_secrets(server: str, config: dict, *, headers: bool = False) -> tuple[dict, dict[str, str]]:
    """``config`` with credential values lifted out, and those values by entry name.

    Pure: nothing is stored. Existing references are kept. A credential-named
    value that is blank or not a string stays where it is for the config's own
    validation to judge. Headers are lifted only for ``headers=True``, which the
    one-time migration uses: HTTP MCP config refuses a literal credential header
    on every write path, so a write never lifts one, and the refusal stands.
    """
    check_references(config)
    result = dict(config)
    lifted: dict[str, str] = {}
    fields = ((SECRET_ENV, "env"), (SECRET_HEADERS, "headers")) if headers else ((SECRET_ENV, "env"),)
    for field, source in fields:
        values = _literals(config, source)
        keep: dict[str, Any] = {}
        names = list(_listed(config, field))
        for var, value in values.items():
            movable = (
                isinstance(var, str) and isinstance(value, str) and value.strip()
                and is_credential_name(var)
                and not (source == "headers" and var.lower() in REFUSED_HEADERS)
            )
            if movable:
                lifted[entry_name(server, field, var)] = value
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


def store_secrets(values: dict[str, str], *, db: sqlite3.Connection | None = None) -> None:
    """Write each value to its entry, replacing any earlier one."""
    for name, value in values.items():
        try:
            key_store.set_managed_value(name, value, db=db)
        except key_store.KeyStoreError:
            raise MCPConfigError(
                "a credential value cannot be saved: use printable characters, with no spaces, up to 4096"
            ) from None


def stored_matches(values: dict[str, str], *, db: sqlite3.Connection | None = None) -> bool:
    """Whether the store already holds exactly these values."""
    return key_store.resolve_names(values, db=db) == values


def resolve(
    server: str, config: dict, *, db: sqlite3.Connection | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """The credential ``env`` and ``headers`` values for one launch.

    Raises :class:`MCPConfigError` when a referenced value is no longer in the
    store, naming the variable but never a value.
    """
    check_references(config)
    resolved: dict[str, dict[str, str]] = {SECRET_ENV: {}, SECRET_HEADERS: {}}
    missing: list[str] = []
    for field in _KINDS:
        names = _listed(config, field)
        stored = key_store.resolve_names((entry_name(server, field, var) for var in names), db=db)
        for var in names:
            value = stored.get(entry_name(server, field, var))
            if value is None:
                missing.append(var)
            else:
                resolved[field][var] = value
    if missing:
        raise MCPConfigError(
            f"credential value missing from the key store for: {', '.join(sorted(missing))}; configure the server again"
        )
    return resolved[SECRET_ENV], resolved[SECRET_HEADERS]


def replace_servers(old: dict, new: dict, *, db: sqlite3.Connection | None = None) -> dict:
    """Store the credential values of the new set and return its configs with references.

    Removes the entries the old set referenced and the new one no longer does,
    a removed server included. Pass the settings write's connection: the
    values, the removals and the settings commit together, so a failed write
    leaves every old config resolvable. A reference the new set carries without
    a value must already resolve, so the replacement cannot leave a server
    unable to start. Nothing is stored when any server is refused.
    """
    cleaned: dict[str, dict] = {}
    lifted: dict[str, str] = {}
    for server, config in new.items():
        cleaned[server], values = split_secrets(server, config)
        lifted.update(values)
    referenced = set().union(*(referenced_entries(server, config) for server, config in cleaned.items()))
    unresolved = referenced - set(lifted) - set(key_store.resolve_names(referenced - set(lifted), db=db))
    if unresolved:
        raise MCPConfigError("a credential reference has no value in the key store; include the value")
    store_secrets(lifted, db=db)
    previous = set()
    for server, config in old.items():
        try:
            previous |= referenced_entries(server, config) if isinstance(config, dict) else set()
        except MCPConfigError:
            continue
    for name in sorted(previous - referenced):
        key_store.drop_managed_entry(name, db=db)
    return cleaned
