"""Server keys in the key store.

An External endpoint's token or an oMLX server's key is the entry
``server-<server id>``. Add/edit routes and local oMLX setup write it through
here, removing a server drops it, and a reader resolves it with
:func:`resolve_server`. Nothing reads it from ``network_servers.metadata_json``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from infra import key_store

#: The prefix and id shape live in ``key_store`` so its name rules can own them.
PREFIX = key_store.SERVER_PREFIX


def entry_name(server_id: str) -> str:
    """The key store entry holding ``server_id``'s key."""
    name = f"{PREFIX}{server_id}"
    if not key_store.is_managed_name(name):
        raise key_store.KeyStoreError("No key has that name.")
    return name


def resolve_server(server_id: str | None, *, db: sqlite3.Connection | None = None) -> str | None:
    """The stored key for a server, or None when none is set (or the id is not a server's)."""
    if not server_id:
        return None
    try:
        name = entry_name(server_id)
    except key_store.KeyStoreError:
        return None
    return key_store.resolve_names([name], db=db).get(name)


def server_keys(server_ids: Iterable[str], *, db: sqlite3.Connection | None = None) -> dict[str, str]:
    """Every listed server that has a key, by server id, from one read of the store."""
    names: dict[str, str] = {}
    for server_id in server_ids:
        try:
            names[entry_name(server_id)] = server_id
        except key_store.KeyStoreError:
            continue
    stored = key_store.resolve_names(names, db=db)
    return {names[name]: value for name, value in stored.items()}


def clean_key(value: object) -> str:
    """The key as the store would keep it, or :class:`KeyStoreError`; checked before a probe or write."""
    return key_store.validated_value(value)


def set_server_key(server_id: str, value: object, *, db: sqlite3.Connection | None = None) -> None:
    """Store a server's key, replacing any earlier one. Server code only: no route calls it."""
    key_store.set_managed_value(entry_name(server_id), value, db=db)


def remove_server_key(server_id: str, *, db: sqlite3.Connection | None = None) -> None:
    """Drop a server's key, entry and value. Removing an absent one is fine."""
    key_store.drop_managed_entry(entry_name(server_id), db=db)


def server_key_rows(servers: Iterable[dict]) -> list[dict]:
    """Status rows for Settings › Keys, one per server listed: never a value."""
    listed = [(str(s["id"]), str(s["instance_name"])) for s in servers]
    stored = server_keys(server_id for server_id, _ in listed)
    return [
        {"name": f"{PREFIX}{server_id}", "server_id": server_id, "label": label, "is_set": server_id in stored}
        for server_id, label in listed
    ]
