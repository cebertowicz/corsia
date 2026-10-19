"""Schema for provider-connected accounts, separate from hosted connected apps."""

from __future__ import annotations

# Audit history keeps the account id without a foreign key: Remove deletes the
# account row, and its audit record must outlive it even when SQLite foreign
# keys are on.
AUDIT_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS connected_account_audit (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        account_id TEXT NOT NULL,
        actor_kind TEXT NOT NULL CHECK(actor_kind IN ('person', 'agent', 'system')),
        actor_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('connect', 'sign_in', 'sign_out', 'remove')),
        revocation_cause TEXT CHECK(revocation_cause IS NULL OR revocation_cause IN (
            'revoked', 'provider_unreachable', 'provider_timeout',
            'provider_rejected', 'provider_unexpected', 'not_signed_in'
        ))
    )"""

CONNECTED_ACCOUNTS_SCHEMA_SQL = f"""
    CREATE TABLE IF NOT EXISTS connected_accounts (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        provider_subject TEXT,
        address TEXT NOT NULL,
        capabilities_json TEXT NOT NULL CHECK(json_valid(capabilities_json)),
        state TEXT NOT NULL CHECK(state IN ('connected', 'needs_sign_in', 'signed_out')),
        reach_mode TEXT NOT NULL CHECK(reach_mode IN ('all', 'selected', 'corsia')),
        outside_projects INTEGER NOT NULL DEFAULT 0
            CHECK(outside_projects IN (0, 1)),
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
    );
    CREATE INDEX IF NOT EXISTS idx_connected_accounts_provider
        ON connected_accounts(provider, state);
    CREATE TABLE IF NOT EXISTS connected_account_reach (
        account_id TEXT NOT NULL REFERENCES connected_accounts(id) ON DELETE CASCADE,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        PRIMARY KEY(account_id, project_id)
    );
    CREATE INDEX IF NOT EXISTS idx_connected_account_reach_project
        ON connected_account_reach(project_id, account_id);
{AUDIT_TABLE_SQL};
    CREATE INDEX IF NOT EXISTS idx_connected_account_audit_account
        ON connected_account_audit(account_id, id);
"""
