"""Schema 136 -> 137 creates declared-event intake receipts safely."""
from __future__ import annotations

import sqlite3

from infra.migration_136_to_137_event_intake import migrate_136_to_137


def _intake_columns() -> set[str]:
    return {
        "id", "event_id", "outcome", "rules_matched", "refusal_code", "producer",
        "origin", "event_type", "at",
    }


def test_intake_receipt_migration_is_idempotent_and_stays_in_the_open_transaction():
    db = sqlite3.connect(":memory:")
    db.execute("BEGIN")
    migrate_136_to_137(db)
    migrate_136_to_137(db)
    assert db.in_transaction
    columns = {row[1] for row in db.execute("PRAGMA table_info(event_intake_records)")}
    assert columns == _intake_columns()
    fresh = sqlite3.connect(":memory:")
    from infra.connection_schema import CONNECTION_SCHEMA_SQL
    fresh.executescript(CONNECTION_SCHEMA_SQL)
    assert {row[1] for row in fresh.execute("PRAGMA table_info(event_intake_records)")} == _intake_columns()
    fresh.close()
    db.rollback()
    assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_intake_records'").fetchone() is None


def test_intake_schema_allows_refusal_then_acceptance_but_only_one_acceptance():
    migrated = sqlite3.connect(":memory:")
    fresh = sqlite3.connect(":memory:")
    migrate_136_to_137(migrated)
    from infra.connection_schema import CONNECTION_SCHEMA_SQL
    fresh.executescript(CONNECTION_SCHEMA_SQL)

    for db in (migrated, fresh):
        db.execute(
            "INSERT INTO event_intake_records "
            "(event_id,outcome,rules_matched,at) VALUES ('accepted-id','accepted',0,'now')"
        )
        try:
            db.execute(
                "INSERT INTO event_intake_records "
                "(event_id,outcome,rules_matched,at) VALUES ('accepted-id','accepted',0,'later')"
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("accepted event IDs must be unique")
        db.execute(
            "INSERT INTO event_intake_records "
            "(event_id,outcome,rules_matched,at) VALUES ('retry-id','refused',0,'now')"
        )
        db.execute(
            "INSERT INTO event_intake_records "
            "(event_id,outcome,rules_matched,at) VALUES ('retry-id','accepted',0,'later')"
        )
        assert db.execute(
            "SELECT outcome FROM event_intake_records WHERE event_id='retry-id' ORDER BY id"
        ).fetchall() == [("refused",), ("accepted",)]
        db.close()
