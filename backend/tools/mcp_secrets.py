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
# Proxy credentials are never safe to replay to an imported app. Other
# credential-named headers are lifted into the key store rather than retained
# in the app configuration.
REFUSED_HEADERS = frozenset({"proxy-authorization"})

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
        raise MCPConfigError("proxy authorization headers are not supported")


def split_secrets(server: str, config: dict, *, headers: bool = False) -> tuple[dict, dict[str, str]]:
    """``config`` with credential values lifted out, and those values by entry name.

    Pure: nothing is stored. Existing references are kept. A credential-named
    value that is blank or not a string stays where it is for the config's own
    validation to judge. Import callers pass ``headers=True`` so Authorization,
    Cookie and other credential-named headers become managed references.
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


# Values are written only through the Settings > Keys API.

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


# Server-set replacement belonged to the retired tools.mcpServers writer path.
