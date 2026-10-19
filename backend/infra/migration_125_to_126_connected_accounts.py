"""Schema 125 -> 126: introduce connected accounts and migrate legacy Google."""

from __future__ import annotations

import json
import sqlite3
import uuid

from connections.connected_accounts import (
    GOOGLE_CAPABILITIES,
    GOOGLE_PROVIDER,
    credential_key,
    corsia_only_reach,
)
from infra import key_store, key_store_values
from infra.connected_accounts_schema import CONNECTED_ACCOUNTS_SCHEMA_SQL
from infra.settings import get_setting
from infra.time import utc_now_iso

LEGACY_ACCOUNT_SETTING = "google_account"
LEGACY_CURSOR_SETTING = "gmail_poll_cursor"
JOURNAL_KEY = "google-singleton-v1"


def migrate_125_to_126(conn: sqlite3.Connection) -> None:
    """Move one usable legacy Google singleton atomically into its account row.

    The surrounding migration runner owns the transaction and journaled schema
    version. This step writes its own completion marker in that same transaction:
    before commit only the legacy entries resolve; after commit only the account
    bundle resolves. A failed bundle write therefore leaves the old path intact.
    """
    conn.executescript(CONNECTED_ACCOUNTS_SCHEMA_SQL)
    if conn.execute(
        "SELECT 1 FROM connected_account_migration_journal WHERE migration_key=?", (JOURNAL_KEY,),
    ).fetchone() is not None:
        return

    legacy = get_setting(LEGACY_ACCOUNT_SETTING, db=conn)
    if legacy is None:
        _journal(conn, None)
        return
    address = legacy.get("email")
    if not isinstance(address, str) or not address.strip():
        raise ValueError("legacy Google account has no address")
    credentials = key_store.resolve_names(key_store.GOOGLE_ENTRIES, db=conn)
    if set(credentials) != set(key_store.GOOGLE_ENTRIES):
        raise ValueError("legacy Google account has incomplete credentials")
    status = legacy.get("status")
    state = status if status in {"connected", "needs_sign_in"} else "signed_out"
    cursor = get_setting(LEGACY_CURSOR_SETTING, db=conn)
    account_id = str(uuid.uuid4())
    now = utc_now_iso()
    reach = corsia_only_reach()
    key = credential_key(account_id)
    bundle = {
        "client_id": credentials[key_store.GOOGLE_CLIENT_ID],
        "client_secret": credentials[key_store.GOOGLE_CLIENT_SECRET],
        "refresh_token": credentials[key_store.GOOGLE_REFRESH_TOKEN],
        "scopes": sorted(scope for scope in legacy.get("scopes", []) if isinstance(scope, str)),
    }

    conn.execute(
        "INSERT INTO connected_accounts("
        "id,provider,provider_subject,address,capabilities_json,state,reach_mode,outside_projects,"
        "credential_key,cursor_json,created_by_kind,created_by_id,signed_in_by_kind,signed_in_by_id,"
        "created_at,updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, GOOGLE_PROVIDER, None, address.strip(),
         json.dumps(GOOGLE_CAPABILITIES, separators=(",", ":")), state, reach.mode,
         int(reach.outside_projects), key,
         json.dumps(cursor, separators=(",", ":"), sort_keys=True) if cursor is not None else None,
         "system", "legacy_google_migration", "system", "legacy_google_migration", now, now),
    )
    ref = key_store_values.put(conn, key, json.dumps(bundle, separators=(",", ":"), sort_keys=True))
    conn.execute(
        "INSERT INTO key_store_entries(name,label,description,created_at,set_at,value_ref) VALUES (?,?,?,?,?,?)",
        (key, "Google account", "Credential bundle for one connected Google account.", now, now, ref),
    )
    resolved = key_store_values.resolve(conn, [key]).get(key)
    if resolved != json.dumps(bundle, separators=(",", ":"), sort_keys=True):
        raise ValueError("new Google account credentials could not be verified")

    conn.execute("DELETE FROM key_store_entries WHERE name IN (?,?,?)", key_store.GOOGLE_ENTRIES)
    conn.execute("DELETE FROM settings WHERE key IN (?,?)", (LEGACY_ACCOUNT_SETTING, LEGACY_CURSOR_SETTING))
    key_store_values.sweep_after_commit(conn)
    _journal(conn, account_id)


def _journal(conn: sqlite3.Connection, account_id: str | None) -> None:
    conn.execute(
        "INSERT INTO connected_account_migration_journal(migration_key,completed_at,account_id) VALUES (?,?,?)",
        (JOURNAL_KEY, utc_now_iso(), account_id),
    )
