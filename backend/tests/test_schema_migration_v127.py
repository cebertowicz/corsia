"""Schema 126 -> 127: connected-account history drops its foreign keys to the account."""

from __future__ import annotations

import sqlite3

from connections.connected_accounts import corsia_only_reach, create_google_account
from connections.google import account_actions
from infra.database import get_db
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_126_to_127_account_history import migrate_126_to_127
from infra.schema import SCHEMA_VERSION

HISTORY = ("connected_account_audit", "connected_account_migration_journal")

# The history tables as schema 126 created them.
LINKED_HISTORY_SQL = """
    CREATE TABLE connected_account_audit (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        account_id TEXT NOT NULL REFERENCES connected_accounts(id) ON DELETE CASCADE,
        actor_kind TEXT NOT NULL CHECK(actor_kind IN ('person', 'agent', 'system')),
        actor_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('connect', 'sign_in', 'sign_out', 'remove')),
        revocation_cause TEXT
    );
    CREATE INDEX idx_connected_account_audit_account ON connected_account_audit(account_id, id);
    CREATE TABLE connected_account_migration_journal (
        migration_key TEXT PRIMARY KEY,
        completed_at TEXT NOT NULL,
        account_id TEXT REFERENCES connected_accounts(id)
    );
"""


def _schema_126_with_linked_history(db: sqlite3.Connection, account_id: str) -> None:
    for table in HISTORY:
        db.execute(f"DROP TABLE {table}")
    db.executescript(LINKED_HISTORY_SQL)
    db.execute(
        "INSERT INTO connected_account_audit(created_at, account_id, actor_kind, actor_id, action)"
        " VALUES ('t0', ?, 'system', 'test', 'connect')", (account_id,),
    )
    db.execute("INSERT INTO connected_account_migration_journal VALUES ('google-singleton-v1', 't0', ?)", (account_id,))
    db.execute("PRAGMA user_version=126")
    db.commit()


def _foreign_keys(db: sqlite3.Connection, table: str) -> list:
    return db.execute(f"PRAGMA foreign_key_list({table})").fetchall()


def test_v126_registers_the_history_step() -> None:
    assert SCHEMA_VERSION == 127
    assert MIGRATIONS[126] is migrate_126_to_127
    assert 127 not in MIGRATIONS


def test_a_126_database_keeps_its_remove_audit_after_migrating(isolated_db) -> None:
    account_id = str(create_google_account(
        "person@example.com", reach=corsia_only_reach(), created_by=("system", "test"), state="connected",
    )["id"])
    db = get_db()
    try:
        _schema_126_with_linked_history(db, account_id)
        assert all(_foreign_keys(db, table) for table in HISTORY)

        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 126, 127)
        assert not any(_foreign_keys(db, table) for table in HISTORY)
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_connected_account_audit_account'"
        ).fetchone()

        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        account_actions._remove_locally(account_id, ("person", "owner"), db=db)

        assert db.execute("SELECT COUNT(*) FROM connected_accounts WHERE id=?", (account_id,)).fetchone()[0] == 0
        audits = db.execute(
            "SELECT created_at, actor_kind, action FROM connected_account_audit WHERE account_id=? ORDER BY id",
            (account_id,),
        ).fetchall()
        assert [tuple(row)[1:] for row in audits] == [("system", "connect"), ("person", "remove")]
        assert tuple(audits[0])[0] == "t0"
        assert db.execute("SELECT account_id FROM connected_account_migration_journal").fetchone()[0] == account_id
    finally:
        db.close()


def test_the_step_leaves_history_without_foreign_keys_alone(isolated_db) -> None:
    db = get_db()
    try:
        before = [db.execute(f"SELECT sql FROM sqlite_master WHERE name='{table}'").fetchone()[0] for table in HISTORY]
        migrate_126_to_127(db)
        after = [db.execute(f"SELECT sql FROM sqlite_master WHERE name='{table}'").fetchone()[0] for table in HISTORY]
        assert after == before
    finally:
        db.close()
