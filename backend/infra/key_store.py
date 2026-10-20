"""The key store: keys agents use but can never read.

Values live in one file in CORSIA's private state, a folder no agent route can
open, in the protected shell or under reviewed host access; only
``infra.key_store_values`` reads or writes it. Labels, dates and the reference
to each entry's live value live in the database (``infra.key_store_schema``);
values never do, and never go into the settings table either, so no database
read, backup or settings route can carry one. A value is written under a fresh
reference inside the caller's transaction, so the database commit is the only
commit point: a rolled-back write leaves the entry on its old value.

Nothing here turns an entry's name into its value for a caller. What uses a
key is fixed in code: :func:`resolve_builtin`, :func:`resolve_provider`,
``connections.connected_accounts.resolve_google_credentials`` and
``infra.key_store_servers.resolve_server`` are the only readers, and only a
built-in, a cloud-provider, a Google account or a server entry has one. An entry an agent adds does nothing until code
that uses it ships, and cannot claim a name code owns (``RESERVED_PREFIXES``).

Errors raised here carry fixed text and never a value.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from infra import key_store_values
from infra.database import get_db, write_transaction
from infra.key_store_mcp import OAUTH_VALUE_MAX, PREFIX as MCP_PREFIX, is_mcp_entry_name, is_oauth_entry_name
# VALUES_FILE and keys_dir are re-exported: callers find the values file through this module.
from infra.key_store_values import VALUES_FILE as VALUES_FILE, KeyStoreError, keys_dir as keys_dir
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

#: Where a resolved key comes from: the key store.
SOURCE_STORE = "keys"

#: Entry-name prefixes code owns for the kinds that resolve from the store.
#: ``POST /api/keys`` cannot claim a name under any of them.
PROVIDER_PREFIX = "provider-"
SERVER_PREFIX = "server-"
GOOGLE_PREFIX = "google-"
CONNECTED_ACCOUNT_PREFIX = "connected-account--google--"
RESERVED_PREFIXES = (PROVIDER_PREFIX, SERVER_PREFIX, MCP_PREFIX, GOOGLE_PREFIX, CONNECTED_ACCOUNT_PREFIX)
#: The platform-held Google sign-in: the person's OAuth client and the refresh token it earned.
GOOGLE_CLIENT_ID = f"{GOOGLE_PREFIX}client-id"
GOOGLE_CLIENT_SECRET = f"{GOOGLE_PREFIX}client-secret"
GOOGLE_REFRESH_TOKEN = f"{GOOGLE_PREFIX}refresh-token"
GOOGLE_ENTRIES = (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN)
#: A network server's id: lowercase letters, digits and hyphens, short enough for the entry name.
SERVER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,32}$")
#: Providers that can hold a key, entered in add-provider. The hidden legacy
#: names stay so a saved key survives the move and can still be removed.
PROVIDER_NAMES = frozenset(
    {"anthropic", "openai", "openrouter", "deepseek", "gemini", "groq", "omlx"}
)


@dataclass(frozen=True)
class BuiltinKey:
    """An entry whose use ships in code."""

    name: str
    label: str
    used_by: str
    #: Whether Settings › Keys can run a live check of it.
    checkable: bool = False


WEB_SEARCH = BuiltinKey(
    name="web-search",
    label="Web search",
    used_by="web search",
    checkable=True,
)
GITHUB_WEBHOOK = BuiltinKey(
    name="github-webhook",
    label="GitHub webhook secret",
    used_by="GitHub webhook signature checks",
)
BUILTINS: dict[str, BuiltinKey] = {WEB_SEARCH.name: WEB_SEARCH, GITHUB_WEBHOOK.name: GITHUB_WEBHOOK}


def _db(db: sqlite3.Connection | None) -> sqlite3.Connection:
    return db if db is not None else get_db()


def is_managed_name(name: str) -> bool:
    """Whether code, not a person, owns this entry: a built-in, or a provider's, server's, MCP server's or Google sign-in's key."""
    return (
        name in BUILTINS
        or name in GOOGLE_ENTRIES
        or (name.startswith(CONNECTED_ACCOUNT_PREFIX) and bool(name[len(CONNECTED_ACCOUNT_PREFIX):]))
        or (name.startswith(PROVIDER_PREFIX) and name[len(PROVIDER_PREFIX):] in PROVIDER_NAMES)
        or (name.startswith(SERVER_PREFIX) and bool(SERVER_ID_PATTERN.match(name[len(SERVER_PREFIX):])))
        or is_mcp_entry_name(name)
    )


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


def validated_value(value: object, *, limit: int = VALUE_MAX) -> str:
    """The value as it would be stored, or :class:`KeyStoreError` for one the store refuses."""
    if not isinstance(value, str):
        raise KeyStoreError("Couldn't save that. Nothing was changed.")
    cleaned = value.strip()
    if not cleaned:
        raise KeyStoreError("Paste the key first.")
    if len(cleaned) > limit or not _VALUE_PATTERN.match(cleaned):
        raise KeyStoreError("Couldn't save that. Nothing was changed.")
    return cleaned


def validate_new_entry(name: object, label: object, description: object) -> tuple[str, str, str]:
    """Check an entry an agent adds; return its normalized fields."""
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise KeyStoreError("A key name is 1-40 lowercase letters, digits or hyphens.")
    if name.startswith(RESERVED_PREFIXES):
        raise KeyStoreError("A key with that name already exists.")
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


def ensure_managed_entry(
    name: str, label: str, description: str = "", *, db: sqlite3.Connection | None = None,
) -> None:
    """Make one code-owned key visible for a person to set, without a value."""
    if not is_managed_name(name):
        raise KeyStoreError("No key has that name.")
    _name, clean_label, clean_description = validate_new_entry("person-key", label, description)
    now = utc_now_iso()
    with write_transaction(db, scope="key_store_managed_entry") as conn:
        row = _row(name, conn)
        if row is None:
            conn.execute(
                "INSERT INTO key_store_entries (name,label,description,created_at) VALUES (?,?,?,?)",
                (name, clean_label, clean_description, now),
            )
        elif row["label"] is None:
            conn.execute(
                "UPDATE key_store_entries SET label=?,description=? WHERE name=?",
                (clean_label, clean_description, name),
            )


def set_value(name: str, value: object, *, db: sqlite3.Connection | None = None) -> None:
    """Store ``value`` for an existing entry, replacing any earlier one.

    The settings table is never read or written.
    """
    _require_entry(name, _db(db))
    _store(name, validated_value(value), db)


def set_managed_value(name: str, value: object, *, db: sqlite3.Connection | None = None) -> None:
    """Store a value for an entry code owns. Server code only: no route calls it."""
    if not is_managed_name(name):
        raise KeyStoreError("No key has that name.")
    _store(name, validated_value(value, limit=OAUTH_VALUE_MAX if is_oauth_entry_name(name) else VALUE_MAX), db)


def _store(name: str, cleaned: str, db: sqlite3.Connection | None) -> None:
    now = utc_now_iso()
    with write_transaction(db, scope="key_store_set") as conn:
        ref = key_store_values.put(conn, name, cleaned)
        conn.execute(
            "INSERT INTO key_store_entries (name, created_at, set_at, value_ref) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET set_at = excluded.set_at, value_ref = excluded.value_ref",
            (name, now, now, ref),
        )


def _delete_row(name: str, db: sqlite3.Connection | None, scope: str) -> None:
    """Delete an entry's row; its value goes in the sweep after the commit."""
    with write_transaction(db, scope=scope) as conn:
        conn.execute("DELETE FROM key_store_entries WHERE name = ?", (name,))
        key_store_values.sweep_after_commit(conn)


def drop_managed_entry(name: str, *, db: sqlite3.Connection | None = None) -> None:
    """Remove an entry code owns, value and row. Server code only: no route calls it."""
    if not is_managed_name(name) or name in BUILTINS:
        raise KeyStoreError("No key has that name.")
    _delete_row(name, db, "key_store_drop")


def clear_value(name: str, *, db: sqlite3.Connection | None = None) -> None:
    """Remove an entry's stored value, keeping the entry."""
    _require_entry(name, _db(db))
    with write_transaction(db, scope="key_store_clear") as conn:
        conn.execute("UPDATE key_store_entries SET set_at = NULL, value_ref = NULL WHERE name = ?", (name,))
        key_store_values.sweep_after_commit(conn)


def remove_entry(name: str, *, db: sqlite3.Connection | None = None) -> None:
    """Remove an added entry and its value. A built-in entry cannot be removed."""
    if name in BUILTINS:
        raise KeyStoreError("A built-in key can be cleared but not removed.")
    if is_managed_name(name):
        drop_managed_entry(name, db=db)
        return
    _require_entry(name, _db(db))
    _delete_row(name, db, "key_store_remove")


def resolve_builtin(builtin: BuiltinKey, *, db: sqlite3.Connection | None = None) -> tuple[str, str] | None:
    """Return ``(value, source)`` for a built-in's key, or None when none is set.

    Read from the store on every call; the environment is not consulted. The
    value is for the one request that uses it and is never written to the
    environment.
    """
    stored = resolve_names([builtin.name], db=db).get(builtin.name)
    return (stored, SOURCE_STORE) if stored else None


def resolve_provider(name: str, *, db: sqlite3.Connection | None = None) -> str | None:
    """The stored key for a cloud provider, or None when none is set."""
    if name not in PROVIDER_NAMES:
        return None
    return resolve_names([f"{PROVIDER_PREFIX}{name}"], db=db).get(f"{PROVIDER_PREFIX}{name}")


def resolve_names(names: Iterable[str], *, db: sqlite3.Connection | None = None) -> dict[str, str]:
    """The value each named entry's committed row references, for those that have one. Server code only.

    Inside a transaction on ``db`` it reads that transaction's own rows.
    """
    return key_store_values.resolve(_db(db), names)


def _metadata(name: str, row: sqlite3.Row | None, is_set: bool) -> dict:
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
        "is_set": is_set,
        "set_at": row["set_at"] if row is not None and is_set else None,
        "checkable": builtin is not None and builtin.checkable,
    }


def list_entries(db: sqlite3.Connection | None = None) -> list[dict]:
    """Every entry's metadata, built-ins first. Never a value."""
    conn = _db(db)
    rows = {
        row["name"]: row for row in conn.execute(
            "SELECT name, label, description, added_by_thread_id, added_by_name,"
            " created_at, set_at FROM key_store_entries ORDER BY created_at, name"
        ).fetchall()
    }
    stored_names = set(key_store_values.resolve(conn, [*BUILTINS, *rows]))
    entries = [
        _metadata(name, rows.get(name), name in stored_names)
        for name in BUILTINS
    ]
    entries.extend(
        _metadata(name, row, name in stored_names)
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
