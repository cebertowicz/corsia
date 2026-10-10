"""Add immutable user-recorded subscription billing facts."""
from __future__ import annotations


def migrate_94_to_95(conn) -> None:
    """Add billing state without inferring facts for existing accounts."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vendor_account_billing_revisions (
            account_id TEXT PRIMARY KEY REFERENCES vendor_accounts(id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
        );
        CREATE TABLE IF NOT EXISTS vendor_account_billing_facts (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES vendor_accounts(id) ON DELETE RESTRICT,
            kind TEXT NOT NULL CHECK (kind IN ('subscription_rate','overage_paid')),
            amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            provenance TEXT NOT NULL DEFAULT 'user_recorded' CHECK (provenance='user_recorded'),
            account_revision INTEGER NOT NULL CHECK (account_revision >= 0),
            receipt_revision INTEGER,
            receipt_login_seq INTEGER,
            supersedes_id TEXT REFERENCES vendor_account_billing_facts(id),
            created_at TEXT NOT NULL,
            CHECK (start_date < end_date),
            CHECK ((receipt_revision IS NULL AND receipt_login_seq IS NULL)
                OR (receipt_revision >= 0 AND receipt_login_seq >= 0))
        );
        CREATE INDEX IF NOT EXISTS idx_vendor_account_billing_facts_account
            ON vendor_account_billing_facts(account_id,kind,start_date,end_date,created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_vendor_account_billing_facts_supersedes
            ON vendor_account_billing_facts(supersedes_id) WHERE supersedes_id IS NOT NULL;
    """)
