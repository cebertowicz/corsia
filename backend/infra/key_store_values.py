"""The key store's values file: the only code that reads or writes it.

The file maps a reference to a value. Every write mints a fresh reference and
never overwrites an old one; ``key_store_entries.value_ref`` names the live
one, so the database commit is the only commit point. A rolled-back write
leaves its row on the old reference, whose value is still in the file, so a
key is never paired with a host or config it was not committed with.

:func:`sweep` deletes every value no committed row references. It runs inside
its own write transaction: SQLite admits one writer at a time, and every
reference is written inside the write transaction that will point a row at it,
so while the sweep holds the write lock no transaction that wrote a reference
is still open, in this process or any other. It runs after each key store
commit and once at startup, after migrations.

The folder is 0700 and the file 0600, and every write goes to a temporary file
created 0600 in the same call that opens it, flushed to disk, then renamed into
place, so the default umask never leaves a readable window. Errors carry fixed
text and never a value.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

from infra import write_effects
from infra.database import write_transaction
from infra.fs_durability import fsync_directory

log = logging.getLogger(__name__)

VALUES_FILE = "values.json"
#: Separates an entry name from a reference's random part. Entry names never
#: contain it, so a key without it is a name from the earlier flat format.
_REF_SEPARATOR = "."
_SWEEP = "key_store_sweep"

#: Held across each read-modify-write of the file, and by readers across the
#: row read and the file read, so a sweep cannot remove a reference between them.
_lock = threading.Lock()


class KeyStoreError(ValueError):
    """A key store request was refused. The text is safe to show; it never holds a value."""


def keys_dir() -> Path:
    """The private folder holding the values file, resolved at call time."""
    from infra import config

    return Path(config.STATE_ROOT) / "keys"


def _ensure_dir() -> Path:
    folder = keys_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


def _read() -> dict[str, str]:
    try:
        raw = (keys_dir() / VALUES_FILE).read_bytes()
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


def _write(values: dict[str, str]) -> None:
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


def put(conn: sqlite3.Connection, name: str, value: str) -> str:
    """Write ``value`` under a fresh reference for ``name`` and return the reference.

    Call it inside the write transaction that will point ``name``'s row at the
    reference. Until that commits the value is unreferenced, and the sweep, which
    waits for the write lock this transaction holds, cannot see it.
    """
    if not conn.in_transaction:
        raise RuntimeError("a key value is written inside the transaction that references it")
    sweep_after_commit(conn)  # raises before any write when no write_transaction owns conn
    ref = f"{name}{_REF_SEPARATOR}{secrets.token_hex(8)}"
    with _lock:
        values = _read()
        values[ref] = value
        _write(values)
    return ref


def sweep_after_commit(conn: sqlite3.Connection) -> None:
    """Sweep once the write transaction open on ``conn`` commits; a rollback skips it."""
    write_effects.after_commit_merged(conn, _SWEEP, [], lambda _items: sweep(conn))


def resolve(conn: sqlite3.Connection, names: Iterable[str]) -> dict[str, str]:
    """The value each named row's reference holds, for the rows that have one."""
    wanted = list(dict.fromkeys(names))
    if not wanted:
        return {}
    marks = ",".join("?" * len(wanted))
    with _lock:
        rows = conn.execute(
            f"SELECT name, value_ref FROM key_store_entries WHERE name IN ({marks}) AND value_ref IS NOT NULL",
            wanted,
        ).fetchall()
        values = _read()
    return {name: values[ref] for name, ref in rows if values.get(ref)}


def store_missing(conn: sqlite3.Connection, values: dict[str, str], now: str) -> list[str]:
    """Store each value whose entry has none yet, row and all; return the names stored. For migrations."""
    stored = []
    for name in sorted(values):
        if conn.execute(
            "SELECT 1 FROM key_store_entries WHERE name = ? AND value_ref IS NOT NULL", (name,),
        ).fetchone():
            continue
        ref = put(conn, name, values[name])
        conn.execute(
            "INSERT INTO key_store_entries (name, created_at, set_at, value_ref) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET set_at = COALESCE(set_at, excluded.set_at),"
            " value_ref = excluded.value_ref",
            (name, now, now, ref),
        )
        stored.append(name)
    return stored


def flat_values() -> dict[str, str]:
    """Values the earlier flat format kept under the entry name itself. For the 106 -> 107 step."""
    with _lock:
        return {name: value for name, value in _read().items() if _REF_SEPARATOR not in name}


def sweep(conn: sqlite3.Connection | None = None) -> int:
    """Delete every value no committed row references; return how many went.

    ``conn`` must not be inside a transaction: the sweep needs the write lock to
    itself, or it could not tell an open transaction's reference from a leftover.
    """
    if conn is not None and conn.in_transaction:
        raise RuntimeError("the key store sweep runs outside any transaction")
    with write_transaction(conn, scope="key_store_sweep") as db:
        live = {row[0] for row in db.execute(
            "SELECT value_ref FROM key_store_entries WHERE value_ref IS NOT NULL"
        )}
        with _lock:
            values = _read()
            kept = {ref: value for ref, value in values.items() if ref in live}
            if len(kept) < len(values):
                _write(kept)
    return len(values) - len(kept)


def sweep_at_startup() -> None:
    """The startup sweep, after migrations. An unreadable file is logged, not fatal: only key use needs it."""
    try:
        removed = sweep()
    except KeyStoreError:
        log.warning("key store startup sweep skipped: the key store file could not be read")
        return
    if removed:
        log.info("key store startup sweep removed %d unreferenced value(s)", removed)
