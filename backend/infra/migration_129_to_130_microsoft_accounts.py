"""Schema 129 -> 130: Microsoft connected-account metadata and fixed audit causes."""
from __future__ import annotations

import sqlite3


# This is deliberately the v130 table shape. Future connected-account fields
# must use their own migration rather than changing this historical fixture.
_CONNECTED_ACCOUNTS_V130_SQL = """
    CREATE TABLE connected_accounts_new (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        provider_subject TEXT,
        account_kind TEXT CHECK(account_kind IS NULL OR account_kind IN ('personal', 'work')),
        state_cause TEXT CHECK(state_cause IS NULL OR state_cause IN (
            'admin_approval_required', 'consent_cancelled', 'provider_unavailable',
            'refresh_token_invalid', 'registration_misconfigured'
        )),
        address TEXT NOT NULL,
        capabilities_json TEXT NOT NULL CHECK(json_valid(capabilities_json)),
        state TEXT NOT NULL CHECK(state IN ('connected', 'needs_sign_in', 'signed_out')),
        reach_mode TEXT NOT NULL CHECK(reach_mode IN ('all', 'selected', 'corsia')),
        outside_projects INTEGER NOT NULL DEFAULT 0 CHECK(outside_projects IN (0, 1)),
        credential_key TEXT NOT NULL UNIQUE,
        cursor_json TEXT,
        created_by_kind TEXT NOT NULL CHECK(created_by_kind IN ('person', 'agent', 'system')),
        created_by_id TEXT NOT NULL,
        signed_in_by_kind TEXT CHECK(signed_in_by_kind IN ('person', 'agent', 'system')),
        signed_in_by_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK((signed_in_by_kind IS NULL) = (signed_in_by_id IS NULL)),
        UNIQUE(provider, provider_subject)
    )
"""


_AUDIT_V130_SQL = """
    CREATE TABLE connected_account_audit_new (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        account_id TEXT NOT NULL,
        actor_kind TEXT NOT NULL CHECK(actor_kind IN ('person', 'agent', 'system')),
        actor_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('connect', 'sign_in', 'sign_out', 'remove')),
        revocation_cause TEXT CHECK(revocation_cause IS NULL OR revocation_cause IN (
            'manual_only', 'not_signed_in', 'provider_rejected', 'provider_timeout', 'provider_unexpected',
            'provider_unreachable', 'revoked'
        ))
    )
"""


def migrate_129_to_130(conn: sqlite3.Connection) -> None:
    """Rebuild account tables to add Microsoft metadata without changing historical v129.

    The account row must be rebuilt because SQLite cannot add the new checked
    columns in the same shape as a fresh v130 database. Reach rows are held in
    memory while their foreign-key table is recreated, then restored unchanged.
    """
    reach_rows = list(conn.execute("SELECT account_id,project_id FROM connected_account_reach"))
    conn.execute("DROP TABLE connected_account_reach")
    conn.execute(_CONNECTED_ACCOUNTS_V130_SQL)
    conn.execute("""
        INSERT INTO connected_accounts_new(
            id,provider,provider_subject,account_kind,state_cause,address,capabilities_json,state,reach_mode,
            outside_projects,credential_key,cursor_json,created_by_kind,created_by_id,signed_in_by_kind,
            signed_in_by_id,created_at,updated_at
        )
        SELECT id,provider,provider_subject,NULL,NULL,address,capabilities_json,state,reach_mode,
               outside_projects,credential_key,cursor_json,created_by_kind,created_by_id,signed_in_by_kind,
               signed_in_by_id,created_at,updated_at
        FROM connected_accounts
    """)
    conn.execute("DROP TABLE connected_accounts")
    conn.execute("ALTER TABLE connected_accounts_new RENAME TO connected_accounts")
    conn.execute("CREATE INDEX idx_connected_accounts_provider ON connected_accounts(provider, state)")
    conn.execute("""
        CREATE TABLE connected_account_reach (
            account_id TEXT NOT NULL REFERENCES connected_accounts(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            PRIMARY KEY(account_id, project_id)
        )
    """)
    conn.execute("CREATE INDEX idx_connected_account_reach_project ON connected_account_reach(project_id, account_id)")
    conn.executemany("INSERT INTO connected_account_reach(account_id,project_id) VALUES (?,?)", reach_rows)

    conn.execute(_AUDIT_V130_SQL)
    conn.execute("""
        INSERT INTO connected_account_audit_new(id,created_at,account_id,actor_kind,actor_id,action,revocation_cause)
        SELECT id,created_at,account_id,actor_kind,actor_id,action,revocation_cause FROM connected_account_audit
    """)
    conn.execute("DROP TABLE connected_account_audit")
    conn.execute("ALTER TABLE connected_account_audit_new RENAME TO connected_account_audit")
    conn.execute("CREATE INDEX idx_connected_account_audit_account ON connected_account_audit(account_id, id)")
