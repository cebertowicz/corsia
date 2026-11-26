"""Saved items: the Settings › Keys entries an agent may have filled into a page without seeing them.

An item is any entry the person can see and set in Settings › Keys: one added
through ``POST /api/keys`` (a name outside ``key_store.RESERVED_PREFIXES``),
a built-in (web search, the GitHub webhook secret) or a cloud provider's key.
Sign-ins that a connected app, a server, Google or a mail account holds stay
with their own code, and a saved login stays with ``infra.saved_logins``.

What may be shown about an item is a :class:`SavedItem`: its entry name, its
label and whether a value is set; never the value. :func:`item_value_for_fill`
is the one reader of a single item's value, for the server's own fill, and
:func:`item_blanking_values` the one reader of them all, for blanking them out
of what the browser returns to agents (through
``saved_logins.blanking_values``). A test pins which modules may name either.

Errors raised here carry fixed text and never a value.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from infra import key_store, key_store_values
from infra.database import get_db
from infra.key_store_values import KeyStoreError

_NO_ITEM = "No saved item has that name."


@dataclass(frozen=True)
class SavedItem:
    """What may be shown about a saved item. It has no value field, by design."""

    name: str
    label: str
    is_set: bool


def _db(db: sqlite3.Connection | None) -> sqlite3.Connection:
    return db if db is not None else get_db()


def _provider_labels(stored: set[str]) -> dict[str, str]:
    """Each cloud provider's key entry and its Settings › Keys label, for the providers that page shows: every
    supported one, and a legacy (hidden) one only while it still holds a key (`stored` names those set)."""
    from api.provider_meta import PROVIDER_META  # Imported here: plain display data, kept out of infra's imports.

    labels = {
        f"{key_store.PROVIDER_PREFIX}{name}": (str(meta.get("label") or name), bool(meta.get("hidden")))
        for name, meta in PROVIDER_META.items()
        if name in key_store.PROVIDER_NAMES and meta.get("type") == "cloud"
    }
    return {entry: label for entry, (label, hidden) in labels.items() if entry in stored or not hidden}


def _added(name: str) -> bool:
    """An entry a person or agent added: a plain name no code owns."""
    return (bool(key_store.NAME_PATTERN.match(name)) and not name.startswith(key_store.RESERVED_PREFIXES)
            and not key_store.is_managed_name(name) and not key_store.is_login_entry_name(name))


def _entry_items(conn: sqlite3.Connection) -> list[SavedItem]:
    """The built-in and added entries that are items, set or not; provider keys come from _provider_labels."""
    return [
        SavedItem(entry["name"], entry["label"], bool(entry["is_set"]))
        for entry in key_store.list_entries(conn)
        if entry["name"] in key_store.BUILTINS or _added(entry["name"])
    ]


def _provider_entries() -> list[str]:
    return [f"{key_store.PROVIDER_PREFIX}{name}" for name in sorted(key_store.PROVIDER_NAMES)]


def list_items(*, db: sqlite3.Connection | None = None) -> list[SavedItem]:
    """Every item an agent may ask to have filled, set or not, built-ins first. Never a value."""
    conn = _db(db)
    items = _entry_items(conn)
    stored = set(key_store_values.resolve(conn, _provider_entries()))
    providers = _provider_labels(stored)
    items.extend(SavedItem(name, label, name in stored) for name, label in providers.items())
    return items


def get_item(name: object, *, db: sqlite3.Connection | None = None) -> SavedItem:
    """One item's view, or :class:`KeyStoreError` when no item has that name. Never a value."""
    if not isinstance(name, str):
        raise KeyStoreError(_NO_ITEM)
    found = next((item for item in list_items(db=db) if item.name == name), None)
    if found is None:
        raise KeyStoreError(_NO_ITEM)
    return found


def item_value_for_fill(name: object, *, db: sqlite3.Connection | None = None) -> str:
    """The item's value, for the server to fill into a page. The one reader of a single item's value.

    Only the modules a test names may call it. The value goes from here into the
    page and nowhere else: never a tool result, log, event or audit row.
    """
    conn = _db(db)
    item = get_item(name, db=conn)
    value = key_store_values.resolve(conn, [item.name]).get(item.name)
    if not value:
        raise KeyStoreError(_NO_ITEM)
    return value


def item_blanking_values(*, db: sqlite3.Connection | None = None) -> frozenset[str]:
    """Every set item's value, for ``saved_logins.blanking_values`` to blank wherever the browser returns one.

    It runs on every agent browser command, so the provider keys are read once, together with the set entries'
    values, keeping the provider keys list_items shows.
    """
    conn = _db(db)
    names = [item.name for item in _entry_items(conn) if item.is_set]
    stored = key_store_values.resolve(conn, names + _provider_entries())
    shown = set(names) | set(_provider_labels(set(stored)))
    return frozenset(value for name, value in stored.items() if name in shown)
