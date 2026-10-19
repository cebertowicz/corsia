"""Schema 129 -> 130: Microsoft account row metadata and fixed audit causes."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_129_to_130_microsoft_accounts import migrate_129_to_130
from infra.schema import SCHEMA_VERSION


def _v129() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    db.execute("ALTER TABLE connected_accounts DROP COLUMN account_kind")
    db.execute("ALTER TABLE connected_accounts DROP COLUMN state_cause")
    db.execute("DROP TABLE connected_account_audit")
    db.executescript("""
        CREATE TABLE connected_account_audit (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            account_id TEXT NOT NULL,
            actor_kind TEXT NOT NULL CHECK(actor_kind IN ('person', 'agent', 'system')),
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('connect', 'sign_in', 'sign_out', 'remove')),
            revocation_cause TEXT CHECK(revocation_cause IS NULL OR revocation_cause IN (
                'revoked', 'provider_unreachable', 'provider_timeout', 'provider_rejected',
                'provider_unexpected', 'not_signed_in'
            ))
        );
        CREATE INDEX idx_connected_account_audit_account ON connected_account_audit(account_id, id);
    """)
    db.execute("PRAGMA user_version=129")
    db.commit()
    return db


def test_v129_registers_microsoft_account_step() -> None:
    assert SCHEMA_VERSION == 130
    assert MIGRATIONS[129] is migrate_129_to_130


def test_migration_adds_metadata_and_accepts_manual_only_audit() -> None:
    with closing(_v129()) as db:
        db.execute(
            "INSERT INTO connected_account_audit(created_at,account_id,actor_kind,actor_id,action,revocation_cause) "
            "VALUES ('before','account','person','person','sign_out','not_signed_in')"
        )
        db.commit()
        result = migrate_to_current(db, 130)
        assert (result.migrated, result.from_version, result.to_version) == (True, 129, 130)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(connected_accounts)")}
        assert {"account_kind", "state_cause"} <= columns
        assert db.execute(
            "SELECT revocation_cause FROM connected_account_audit WHERE created_at='before'"
        ).fetchone()[0] == "not_signed_in"
        db.execute(
            "INSERT INTO connected_account_audit(created_at,account_id,actor_kind,actor_id,action,revocation_cause) "
            "VALUES ('now','account','person','person','remove','manual_only')"
        )
        db.commit()
        assert not migrate_to_current(db, 130).migrated
