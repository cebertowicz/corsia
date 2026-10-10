"""Vendor subscription identities beneath the existing provider transport."""
from __future__ import annotations

VENDOR_ACCOUNT_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS vendor_accounts (
        id TEXT PRIMARY KEY,
        connection_id TEXT NOT NULL REFERENCES connections(id),
        name TEXT NOT NULL CHECK (length(trim(name)) > 0),
        source_home TEXT,
        runtime_home TEXT,
        is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),
        state TEXT NOT NULL DEFAULT 'signed_out'
            CHECK (state IN ('legacy','signed_in','signed_out','removed')),
        revision INTEGER NOT NULL DEFAULT 0,
        identity_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(identity_json)),
        auth_cleanup_json TEXT CHECK (auth_cleanup_json IS NULL OR
            (json_valid(auth_cleanup_json) AND json_type(auth_cleanup_json)='object')),
        discovery_json TEXT CHECK (discovery_json IS NULL OR json_valid(discovery_json)),
        usage_json TEXT CHECK (usage_json IS NULL OR json_valid(usage_json)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        removed_at TEXT,
        CHECK ((source_home IS NULL AND runtime_home IS NULL) OR
               (source_home IS NOT NULL AND runtime_home IS NOT NULL AND source_home != runtime_home))
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_vendor_accounts_default
        ON vendor_accounts(connection_id) WHERE is_default=1;
    CREATE INDEX IF NOT EXISTS idx_vendor_accounts_connection
        ON vendor_accounts(connection_id,created_at);
    CREATE TABLE IF NOT EXISTS thread_vendor_accounts (
        thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        provider TEXT NOT NULL CHECK (provider IN ('claude-code','codex')),
        account_id TEXT NOT NULL REFERENCES vendor_accounts(id),
        pending_account_id TEXT REFERENCES vendor_accounts(id),
        deferred_move TEXT CHECK (deferred_move IS NULL OR json_valid(deferred_move)),
        PRIMARY KEY(thread_id,provider)
    );
    CREATE TABLE IF NOT EXISTS vendor_account_billing_revisions (
        account_id TEXT REFERENCES vendor_accounts(id) ON DELETE RESTRICT,
        network_server_id TEXT REFERENCES network_servers(id) ON DELETE RESTRICT,
        revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
        CHECK ((account_id IS NULL) != (network_server_id IS NULL))
    );
    CREATE TABLE IF NOT EXISTS vendor_account_billing_facts (
        id TEXT PRIMARY KEY,
        account_id TEXT REFERENCES vendor_accounts(id) ON DELETE RESTRICT,
        network_server_id TEXT REFERENCES network_servers(id) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK (kind IN ('subscription_rate','overage_paid','pay_per_use','price_cleared')),
        amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        charge_day INTEGER CHECK (charge_day IS NULL OR charge_day BETWEEN 1 AND 31),
        provenance TEXT NOT NULL DEFAULT 'user_recorded' CHECK (provenance='user_recorded'),
        account_revision INTEGER CHECK (account_revision IS NULL OR account_revision >= 0),
        receipt_revision INTEGER,
        receipt_login_seq INTEGER,
        supersedes_id TEXT REFERENCES vendor_account_billing_facts(id),
        created_at TEXT NOT NULL,
        CHECK ((account_id IS NULL) != (network_server_id IS NULL)),
        CHECK ((kind = 'subscription_rate') = (charge_day IS NOT NULL)),
        CHECK (start_date < end_date),
        CHECK ((receipt_revision IS NULL AND receipt_login_seq IS NULL)
            OR (receipt_revision >= 0 AND receipt_login_seq >= 0))
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_vendor_account_billing_revisions_account
        ON vendor_account_billing_revisions(account_id) WHERE account_id IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_vendor_account_billing_revisions_server
        ON vendor_account_billing_revisions(network_server_id) WHERE network_server_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_vendor_account_billing_facts_account
        ON vendor_account_billing_facts(account_id,kind,start_date,end_date,created_at) WHERE account_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_vendor_account_billing_facts_server
        ON vendor_account_billing_facts(network_server_id,kind,start_date,end_date,created_at)
        WHERE network_server_id IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_vendor_account_billing_facts_supersedes
        ON vendor_account_billing_facts(supersedes_id) WHERE supersedes_id IS NOT NULL;
"""
