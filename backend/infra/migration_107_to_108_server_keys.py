"""Schema 107 -> 108: move every server key into the key store.

Before this step an External endpoint's token and an oMLX server's key lived in
``network_servers.metadata_json`` under ``api_key``. After it they live only in
the key store as ``server-<server id>``, and nothing reads the old field.

As in the 106 -> 107 step, each value is written under a fresh reference and the
row naming it is written in the runner's transaction, so the commit is the only
commit point. A failure rolls the transaction back and leaves every old copy in
place; what the step wrote to the file is unreferenced, and the next sweep
removes it. A value already in the store wins over the metadata copy.

A removed server's key is dropped, not migrated: its field is deleted and no
entry is created. Only counts are logged.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3

from infra.migration_secure_delete import secure_delete
from infra.time import utc_now_iso

log = logging.getLogger(__name__)

# Copied, not imported, so this step keeps producing the shape it was written for.
_VALUE_PATTERN = re.compile(r"^[\x21-\x7e]{1,4096}$")
_SERVER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,32}$")
_FIELD = "api_key"


def _clean(value: object) -> str | None:
    """The usable text of a stored value, or None when blank or not storable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _VALUE_PATTERN.match(text) else None


def _has_servers_table(conn: sqlite3.Connection) -> bool:
    # Ladder tests start from bare precursor schemas; a real DB always has the table.
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'network_servers'"
    ).fetchone() is not None


def _collect(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, dict], int, dict[str, int]]:
    """Live servers' keys by entry name, the metadata to rewrite by row id, removed rows' keys dropped, and kept counts."""
    keys: dict[str, str] = {}
    rewritten: dict[str, dict] = {}
    dropped = 0
    left = {"value": 0, "server id": 0}
    rows = conn.execute("SELECT id, status, metadata_json FROM network_servers").fetchall()
    for server_id, status, raw in rows:
        try:
            meta = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            continue
        if not isinstance(meta, dict) or _FIELD not in meta:
            continue
        if status == "removed":
            dropped += 1
        else:
            value = _clean(meta[_FIELD])
            blank = isinstance(meta[_FIELD], str) and not meta[_FIELD].strip()
            if value is None and not blank:
                left["value"] += 1
                continue  # not storable: left where it is rather than lost
            if value is not None:
                if not _SERVER_ID_PATTERN.match(str(server_id)):
                    left["server id"] += 1
                    continue  # no entry name can carry it: left where it is
                keys[f"server-{server_id}"] = value
        del meta[_FIELD]
        rewritten[str(server_id)] = meta
    return keys, rewritten, dropped, left


def migrate_107_to_108(conn: sqlite3.Connection) -> None:
    """Move the keys into the store, then drop the metadata fields."""
    from infra import key_store_values

    if not _has_servers_table(conn):
        return
    keys, rewritten, dropped, left = _collect(conn)
    moved = key_store_values.store_missing(conn, keys, utc_now_iso())
    with secure_delete(conn):
        for server_id, meta in rewritten.items():
            conn.execute(
                "UPDATE network_servers SET metadata_json = ? WHERE id = ?",
                (json.dumps(meta), server_id),
            )
    if any(left.values()):
        log.warning(
            "key store migration: left %d server key(s) in place because the key store cannot take them; "
            "unstorable value=%d, unusable server id=%d",
            sum(left.values()), left["value"], left["server id"],
        )
    log.info(
        "key store migration: moved %d server key(s); by kind: servers=%d; dropped %d from removed servers",
        len(moved), len(moved), dropped,
    )
