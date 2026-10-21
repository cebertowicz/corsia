"""Schema 134 -> 135 creates declared-event intake receipts safely."""
from __future__ import annotations

import sqlite3

from infra.migration_134_to_135_event_intake import migrate_134_to_135


def test_intake_receipt_migration_is_idempotent_and_stays_in_the_open_transaction():
    db = sqlite3.connect(":memory:")
    db.execute("BEGIN")
    migrate_134_to_135(db)
    migrate_134_to_135(db)
    assert db.in_transaction
    columns = {row[1] for row in db.execute("PRAGMA table_info(event_intake_records)")}
    assert columns == {"event_id", "outcome", "rules_matched"}
    db.rollback()
    assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_intake_records'").fetchone() is None
