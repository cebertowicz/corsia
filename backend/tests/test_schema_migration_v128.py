"""Schema 127 -> 128: the account audit's `manual_only` cause becomes `not_signed_in`."""

from __future__ import annotations

import sqlite3

import pytest

from infra.database import get_db
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_127_to_128_account_audit_cause import migrate_127_to_128
from infra.schema import SCHEMA_VERSION

# The audit table as schema 127 created it.
AUDIT_127_SQL = """
    CREATE TABLE connected_account_audit (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        account_id TEXT NOT NULL,
        actor_kind TEXT NOT NULL CHECK(actor_kind IN ('person', 'agent', 'system')),
        actor_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('connect', 'sign_in', 'sign_out', 'remove')),
        revocation_cause TEXT CHECK(revocation_cause IS NULL OR revocation_cause IN (
            'revoked', 'provider_unreachable', 'provider_timeout',
            'provider_rejected', 'provider_unexpected', 'manual_only'
        ))
    );
    CREATE INDEX idx_connected_account_audit_account ON connected_account_audit(account_id, id);
"""


def _schema_127(db: sqlite3.Connection) -> None:
    db.execute("DROP TABLE connected_account_audit")
    db.executescript(AUDIT_127_SQL)
    for cause in (None, "revoked", "manual_only", "provider_rejected"):
        db.execute(
            "INSERT INTO connected_account_audit(created_at, account_id, actor_kind, actor_id, action, revocation_cause)"
            " VALUES ('t0', 'acc', 'person', 'steve', 'remove', ?)", (cause,),
        )
    db.execute("PRAGMA user_version=127")
    db.commit()


def _causes(db: sqlite3.Connection) -> list[str | None]:
    return [row[0] for row in db.execute("SELECT revocation_cause FROM connected_account_audit ORDER BY id")]


def test_v127_registers_the_audit_cause_step() -> None:
    assert SCHEMA_VERSION == 128
    assert MIGRATIONS[127] is migrate_127_to_128
    assert 128 not in MIGRATIONS


def test_a_127_database_keeps_every_audit_row_and_renames_the_cause(isolated_db) -> None:
    db = get_db()
    try:
        _schema_127(db)

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 127, 128)
        assert _causes(db) == [None, "revoked", "not_signed_in", "provider_rejected"]
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_connected_account_audit_account'"
        ).fetchone()
        db.execute(
            "INSERT INTO connected_account_audit(created_at, account_id, actor_kind, actor_id, action, revocation_cause)"
            " VALUES ('t1', 'acc', 'person', 'steve', 'remove', 'not_signed_in')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO connected_account_audit(created_at, account_id, actor_kind, actor_id, action, revocation_cause)"
                " VALUES ('t2', 'acc', 'person', 'steve', 'remove', 'manual_only')"
            )
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
    finally:
        db.close()


def test_the_step_leaves_an_audit_table_that_already_has_the_new_cause_alone(isolated_db) -> None:
    db = get_db()
    try:
        before = db.execute("SELECT sql FROM sqlite_master WHERE name='connected_account_audit'").fetchone()[0]
        migrate_127_to_128(db)
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='connected_account_audit'").fetchone()[0] == before
    finally:
        db.close()
