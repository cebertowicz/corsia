"""Schema 128 -> 129: introduce connected accounts and migrate legacy Google."""

from __future__ import annotations

import json
import sqlite3
import uuid

from connections.connected_accounts import (
    GOOGLE_CAPABILITIES,
    GOOGLE_PROVIDER,
    ONLY_CORSIA,
    credential_key,
)
from infra import key_store, key_store_values
from infra.connected_accounts_schema import CONNECTED_ACCOUNTS_SCHEMA_SQL
from infra.settings import get_setting
from infra.time import utc_now_iso

LEGACY_ACCOUNT_SETTING = "google_account"
LEGACY_CURSOR_SETTING = "gmail_poll_cursor"
JOURNAL_KEY = "google-singleton-v1"


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """Whether a legacy table exists at this point in the migration ladder."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _legacy_setting(conn: sqlite3.Connection, key: str) -> object | None:
    """Read a legacy setting only when its table predates this migration."""
    return get_setting(key, db=conn) if _has_table(conn, "settings") else None


def _legacy_credentials(conn: sqlite3.Connection) -> dict[str, str] | None:
    """Return a complete legacy bundle when the old key-store path is usable."""
    if not _has_table(conn, "key_store_entries"):
        return None
    resolved = key_store.resolve_names(key_store.GOOGLE_ENTRIES, db=conn)
    if set(resolved) != set(key_store.GOOGLE_ENTRIES):
        return None
    values = {name: resolved[name] for name in key_store.GOOGLE_ENTRIES}
    return values if all(isinstance(value, str) and value for value in values.values()) else None


def _clear_legacy(conn: sqlite3.Connection) -> None:
    """Retire any surviving singleton data after the new state is durable."""
    if _has_table(conn, "key_store_entries"):
        conn.execute("DELETE FROM key_store_entries WHERE name IN (?,?,?)", key_store.GOOGLE_ENTRIES)
        key_store_values.sweep_after_commit(conn)
    if _has_table(conn, "settings"):
        conn.execute("DELETE FROM settings WHERE key IN (?,?)", (LEGACY_ACCOUNT_SETTING, LEGACY_CURSOR_SETTING))



def migrate_128_to_129(conn: sqlite3.Connection) -> None:
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

    legacy = _legacy_setting(conn, LEGACY_ACCOUNT_SETTING)
    cursor = _legacy_setting(conn, LEGACY_CURSOR_SETTING)
    address = legacy.get("email") if isinstance(legacy, dict) else None
    if not isinstance(legacy, dict) or not isinstance(address, str) or not address.strip():
        _rekey_screens(conn, None, None)
        _clear_legacy(conn)
        _journal(conn, None)
        return

    credentials = _legacy_credentials(conn)
    status = legacy.get("status")
    state = status if credentials is not None and status in {"connected", "needs_sign_in"} else "needs_sign_in"
    account_id = str(uuid.uuid4())
    now = utc_now_iso()
    key = credential_key(account_id)
    conn.execute(
        "INSERT INTO connected_accounts("
        "id,provider,provider_subject,address,capabilities_json,state,reach_mode,outside_projects,"
        "credential_key,cursor_json,created_by_kind,created_by_id,signed_in_by_kind,signed_in_by_id,"
        "created_at,updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, GOOGLE_PROVIDER, None, address.strip(),
         json.dumps(GOOGLE_CAPABILITIES, separators=(",", ":")), state, ONLY_CORSIA, 0, key,
         json.dumps(cursor, separators=(",", ":"), sort_keys=True) if cursor is not None else None,
         "system", "legacy_google_migration", "system", "legacy_google_migration", now, now),
    )
    if credentials is not None:
        bundle = {
            "client_id": credentials[key_store.GOOGLE_CLIENT_ID],
            "client_secret": credentials[key_store.GOOGLE_CLIENT_SECRET],
            "refresh_token": credentials[key_store.GOOGLE_REFRESH_TOKEN],
            "scopes": sorted(scope for scope in legacy.get("scopes", []) if isinstance(scope, str)),
        }
        encoded = json.dumps(bundle, separators=(",", ":"), sort_keys=True)
        ref = key_store_values.put(conn, key, encoded)
        conn.execute(
            "INSERT INTO key_store_entries(name,label,description,created_at,set_at,value_ref) VALUES (?,?,?,?,?,?)",
            (key, "Google account", "Credential bundle for one connected Google account.", now, now, ref),
        )
        if key_store_values.resolve(conn, [key]).get(key) != encoded:
            raise ValueError("new Google account credentials could not be verified")
    _rekey_screens(conn, address.strip(), account_id)
    _rekey_legacy_google_data(conn, account_id)
    _clear_legacy(conn)
    _journal(conn, account_id)


def _rekey_screens(conn: sqlite3.Connection, address: str | None, account_id: str | None) -> None:
    """Key stored screens by the new account id instead of the address; drop any no account owns."""
    if not _has_table(conn, "email_screens"):
        return
    if address is not None and account_id is not None:
        conn.execute(
            "UPDATE OR IGNORE email_screens SET account=? WHERE lower(account)=lower(?)", (account_id, address),
        )
    conn.execute("DELETE FROM email_screens WHERE account IS NOT ?", (account_id,))


def _rekey_legacy_google_data(conn: sqlite3.Connection, account_id: str) -> None:
    """Namespace legacy Gmail thread identities and bind still-open Google action drafts."""
    prefix = f"google:{account_id}:thread:"
    if _has_table(conn, "artifact_identities"):
        conn.execute(
            "UPDATE artifact_identities SET external_id=? || external_id "
            "WHERE integration='gmail' AND type='email-thread' "
            "AND external_id NOT LIKE 'google:%:thread:%'",
            (prefix,),
        )
    if _has_table(conn, "connection_rules"):
        conn.execute(
            "UPDATE connection_rules SET value=? || value "
            "WHERE connector='gmail' AND fact='thread_id' AND value NOT LIKE 'google:%:thread:%'",
            (prefix,),
        )
    if not _has_table(conn, "action_drafts"):
        return
    for row in conn.execute(
        "SELECT id,target_json FROM action_drafts WHERE kind IN ('email','invite') AND state='open'",
    ):
        try:
            target = json.loads(row["target_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(target, dict) or target.get("account_id"):
            continue
        target["account_id"] = account_id
        conn.execute(
            "UPDATE action_drafts SET target_json=? WHERE id=?",
            (json.dumps(target, separators=(",", ":"), sort_keys=True), row["id"]),
        )


def _journal(conn: sqlite3.Connection, account_id: str | None) -> None:
    conn.execute(
        "INSERT INTO connected_account_migration_journal(migration_key,completed_at,account_id) VALUES (?,?,?)",
        (JOURNAL_KEY, utc_now_iso(), account_id),
    )
