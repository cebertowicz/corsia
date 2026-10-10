"""Schema v95 adds empty immutable account billing storage to v94 databases."""
from __future__ import annotations

import sqlite3

from infra.migration_94_to_95_subscription_billing import migrate_94_to_95


def test_v94_to_v95_adds_empty_billing_tables_and_constraints():
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE vendor_accounts (id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO vendor_accounts VALUES ('account')")
    migrate_94_to_95(db)

    db.execute("INSERT INTO vendor_account_billing_revisions VALUES ('account', 0)")
    db.execute("INSERT INTO vendor_account_billing_facts "
               "(id,account_id,kind,amount_cents,start_date,end_date,account_revision,created_at) "
               "VALUES ('fact','account','subscription_rate',0,'2026-02-01','2026-03-01',0,'now')")
    assert db.execute("SELECT COUNT(*) FROM vendor_account_billing_facts").fetchone()[0] == 1
    assert db.execute("PRAGMA index_info(idx_vendor_account_billing_facts_account)").fetchall()
