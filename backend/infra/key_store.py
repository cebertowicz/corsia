"""The key store: keys agents use but can never read.

Values live in one file in CORSIA's private state, a folder no agent route can
open, in the protected shell or under reviewed host access. The folder is 0700
and the file 0600, and every write goes to a temporary file created 0600 in
the same call that opens it, flushed to disk, then renamed into place, so the
default umask never leaves a readable window. Labels and dates live in the
database (``infra.key_store_schema``); values never do, and never go into the
settings table either, so no database read, backup or settings route can
carry one.

Nothing here turns an entry's name into its value for a caller. What uses a
key is fixed in code: :func:`resolve_builtin` is the only reader, and only a
built-in entry has one. An entry an agent adds does nothing until code that
uses it ships.

Errors raised here carry fixed text and never a value.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from infra.database import get_db, write_transaction
from infra.fs_durability import fsync_directory
from infra.time import utc_now_iso

#: Lowercase letters, digits and hyphens, up to 40 characters.
NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,40}$")
LABEL_MAX = 80
DESCRIPTION_MAX = 280
#: Longest value accepted. Generous for any API key, and bounds the file.
VALUE_MAX = 4096
#: Printable ASCII without spaces: anything else cannot travel in a request
#: header, and an HTTP client refusing it could echo it in its error text.
_VALUE_PATTERN = re.compile(r"^[\x21-\x7e]+$")

VALUES_FILE = "values.json"

#: Where a resolved key came from, in the order search reads them.
SOURCE_STORE = "keys"
SOURCE_SETTINGS = "settings"
SOURCE_ENVIRONMENT = "environment"


class KeyStoreError(ValueError):
    """A key store request was refused. The text is safe to show; it never holds a value."""


@dataclass(frozen=True)
class BuiltinKey:
    """An entry whose use ships in code, with today's other places for its key."""

    name: str
    label: str
    used_by: str
    #: The settings table path of the key's older home, read after the store.
    settings_path: tuple[str, ...]
    #: The environment variable read last.
    env_var: str


WEB_SEARCH = BuiltinKey(
    name="web-search",
    label="Web search",
    used_by="web search",
    settings_path=("tools", "web", "search", "apiKey"),
    env_var="BRAVE_API_KEY",
)
BUILTINS: dict[str, BuiltinKey] = {WEB_SEARCH.name: WEB_SEARCH}

_values_lock = threading.Lock()


def keys_dir() -> Path:
    """The private folder holding the values file, resolved at call time."""
    from infra import config

    return Path(config.STATE_ROOT) / "keys"


def _values_path() -> Path:
    return keys_dir() / VALUES_FILE


def _ensure_dir() -> Path:
    folder = keys_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


def _read_values() -> dict[str, str]:
    path = _values_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        # The file is CORSIA's own; an unreadable one is refused, never echoed.
        raise KeyStoreError("The key store file could not be read.") from None
    if not isinstance(parsed, dict):
        raise KeyStoreError("The key store file could not be read.")
    return {str(k): v for k, v in parsed.items() if isinstance(v, str)}


def _write_values(values: dict[str, str]) -> None:
    folder = _ensure_dir()
    temporary = folder / f".{VALUES_FILE}.{secrets.token_hex(8)}.tmp"
    # Created 0600 by the call that opens it; O_EXCL refuses a planted file.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(values, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, folder / VALUES_FILE)
        fsync_directory(folder)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _db(db: sqlite3.Connection | None) -> sqlite3.Connection:
    return db if db is not None else get_db()


def _row(name: str, db: sqlite3.Connection) -> sqlite3.Row | None:
    return db.execute(
        "SELECT name, label, description, added_by_thread_id, added_by_name,"
        " created_at, set_at FROM key_store_entries WHERE name = ?",
        (name,),
    ).fetchone()


def _require_entry(name: str, db: sqlite3.Connection) -> None:
    if name in BUILTINS:
        return
    row = _row(name, db)
    if row is None or row["label"] is None:
        raise KeyStoreError("No key has that name.")


def _validated_value(value: object) -> str:
    if not isinstance(value, str):
        raise KeyStoreError("Couldn't save that. Nothing was changed.")
    cleaned = value.strip()
    if not cleaned:
        raise KeyStoreError("Paste the key first.")
    if len(cleaned) > VALUE_MAX or not _VALUE_PATTERN.match(cleaned):
        raise KeyStoreError("Couldn't save that. Nothing was changed.")
    return cleaned


def validate_new_entry(name: object, label: object, description: object) -> tuple[str, str, str]:
    """Check an entry an agent adds; return its normalized fields."""
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise KeyStoreError("A key name is 1-40 lowercase letters, digits or hyphens.")
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > LABEL_MAX:
        raise KeyStoreError(f"A label is plain text, 1-{LABEL_MAX} characters.")
    if description is None:
        description = ""
    if not isinstance(description, str) or len(description.strip()) > DESCRIPTION_MAX:
        raise KeyStoreError(f"A description is plain text, up to {DESCRIPTION_MAX} characters.")
    return name, label.strip(), description.strip()


def add_entry(
    name: str,
    label: str,
    description: str,
    *,
    added_by_thread_id: str | None,
    added_by_name: str | None,
    db: sqlite3.Connection | None = None,
) -> dict:
    """Add an entry with no value. Refuses a name in use, a built-in's included."""
    name, label, description = validate_new_entry(name, label, description)
    if name in BUILTINS:
        raise KeyStoreError("A key with that name already exists.")
    with write_transaction(db, scope="key_store_add") as conn:
        if _row(name, conn) is not None:
            raise KeyStoreError("A key with that name already exists.")
        conn.execute(
            "INSERT INTO key_store_entries (name, label, description,"
            " added_by_thread_id, added_by_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, label, description, added_by_thread_id, added_by_name, utc_now_iso()),
        )
    return entry_metadata(name, db=db)


def _drop_settings_copy(builtin: BuiltinKey, conn: sqlite3.Connection) -> None:
    """Delete a built-in's older settings copy, if any; the store now holds its key."""
    from infra.settings import get_setting, set_setting

    top = get_setting(builtin.settings_path[0], db=conn)
    node: object = top
    for part in builtin.settings_path[1:-1]:
        node = node.get(part) if isinstance(node, dict) else None
    if isinstance(node, dict) and builtin.settings_path[-1] in node:
        del node[builtin.settings_path[-1]]
        set_setting(builtin.settings_path[0], top, db=conn)


def set_value(name: str, value: object, *, db: sqlite3.Connection | None = None) -> None:
    """Store ``value`` for an existing entry, replacing any earlier one.

    Setting a built-in also deletes its older settings copy, so the key lives
    only in the store.
    """
    _require_entry(name, _db(db))
    cleaned = _validated_value(value)
    with _values_lock:
        values = _read_values()
        values[name] = cleaned
        _write_values(values)
    now = utc_now_iso()
    with write_transaction(db, scope="key_store_set") as conn:
        conn.execute(
            "INSERT INTO key_store_entries (name, created_at, set_at) VALUES (?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET set_at = excluded.set_at",
            (name, now, now),
        )
        if name in BUILTINS:
            _drop_settings_copy(BUILTINS[name], conn)


def clear_value(name: str, *, db: sqlite3.Connection | None = None) -> None:
    """Remove an entry's stored value, keeping the entry."""
    _require_entry(name, _db(db))
    with _values_lock:
        values = _read_values()
        if values.pop(name, None) is not None:
            _write_values(values)
    with write_transaction(db, scope="key_store_clear") as conn:
        conn.execute("UPDATE key_store_entries SET set_at = NULL WHERE name = ?", (name,))


def remove_entry(name: str, *, db: sqlite3.Connection | None = None) -> None:
    """Remove an added entry and its value. A built-in entry cannot be removed."""
    if name in BUILTINS:
        raise KeyStoreError("A built-in key can be cleared but not removed.")
    _require_entry(name, _db(db))
    with _values_lock:
        values = _read_values()
        if values.pop(name, None) is not None:
            _write_values(values)
    with write_transaction(db, scope="key_store_remove") as conn:
        conn.execute("DELETE FROM key_store_entries WHERE name = ?", (name,))


def _settings_value(builtin: BuiltinKey, db: sqlite3.Connection) -> str:
    from infra.settings import get_setting

    node: object = get_setting(builtin.settings_path[0], db=db) or {}
    for part in builtin.settings_path[1:]:
        node = node.get(part) if isinstance(node, dict) else None
    return node.strip() if isinstance(node, str) else ""


def resolve_builtin(
    builtin: BuiltinKey, *, db: sqlite3.Connection | None = None,
) -> tuple[str, str] | None:
    """Return ``(value, source)`` for a built-in's key, or None when none is set.

    Read on every call, in order: the store, the settings value, the
    environment. A key saved in Settings › Keys always wins. The value is for
    the one request that uses it and is never written to the environment.
    """
    with _values_lock:
        stored = _read_values().get(builtin.name)
    if stored:
        return stored, SOURCE_STORE
    from_settings = _settings_value(builtin, _db(db))
    if from_settings:
        return from_settings, SOURCE_SETTINGS
    from_env = os.environ.get(builtin.env_var, "").strip()
    if from_env:
        return from_env, SOURCE_ENVIRONMENT
    return None


def _source(name: str, stored_names: set[str], db: sqlite3.Connection) -> str | None:
    if name in stored_names:
        return SOURCE_STORE
    builtin = BUILTINS.get(name)
    if builtin is None:
        return None
    if _settings_value(builtin, db):
        return SOURCE_SETTINGS
    if os.environ.get(builtin.env_var, "").strip():
        return SOURCE_ENVIRONMENT
    return None


def _metadata(name: str, row: sqlite3.Row | None, source: str | None) -> dict:
    builtin = BUILTINS.get(name)
    if builtin is not None:
        label, description = builtin.label, ""
    elif row is not None:
        label, description = row["label"], row["description"] or ""
    else:
        raise KeyStoreError("No key has that name.")
    return {
        "name": name,
        "label": label,
        "description": description,
        "builtin": builtin is not None,
        "used_by": builtin.used_by if builtin else None,
        "added_by": None if builtin or row is None else row["added_by_name"],
        "added_by_thread_id": None if builtin or row is None else row["added_by_thread_id"],
        "is_set": source is not None,
        "source": source,
        "set_at": row["set_at"] if row is not None and source == SOURCE_STORE else None,
        "checkable": builtin is not None,
    }


def list_entries(db: sqlite3.Connection | None = None) -> list[dict]:
    """Every entry's metadata, built-ins first. Never a value."""
    conn = _db(db)
    with _values_lock:
        stored_names = set(_read_values())
    rows = {
        row["name"]: row for row in conn.execute(
            "SELECT name, label, description, added_by_thread_id, added_by_name,"
            " created_at, set_at FROM key_store_entries ORDER BY created_at, name"
        ).fetchall()
    }
    entries = [
        _metadata(name, rows.get(name), _source(name, stored_names, conn))
        for name in BUILTINS
    ]
    entries.extend(
        _metadata(name, row, _source(name, stored_names, conn))
        for name, row in rows.items()
        if name not in BUILTINS and row["label"] is not None
    )
    return entries


def entry_metadata(name: str, *, db: sqlite3.Connection | None = None) -> dict:
    """One entry's metadata, or :class:`KeyStoreError` when it doesn't exist."""
    for entry in list_entries(db):
        if entry["name"] == name:
            return entry
    raise KeyStoreError("No key has that name.")
