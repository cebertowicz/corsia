"""Schema 126 -> 127: connected accounts and legacy Google migration."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from connections.connected_accounts import corsia_only_reach, google_accounts, resolve_google_credentials
from infra import key_store, key_store_values
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current
from infra.migration_126_to_127_connected_accounts import migrate_126_to_127
from infra.schema import SCHEMA_VERSION
from infra.settings import get_setting, set_setting


ACCOUNT = "person@example.test"
CURSOR = {"account": ACCOUNT, "floor_at": "old-floor", "history_id": "old-history", "last_success_at": "old-success"}


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    return db


def _dev_126() -> sqlite3.Connection:
    """Reconstruct dev schema 126, including the presence-focus tables, in memory."""
    db = _fresh()
    db.execute("DROP TABLE connected_account_reach")
    db.execute("DROP TABLE connected_account_audit")
    db.execute("DROP TABLE connected_account_migration_journal")
    db.execute("DROP TABLE connected_accounts")
    db.execute("PRAGMA user_version=126")
    db.commit()
    return db


def google_account(*, db):  # type: ignore[no-untyped-def]
    """The single account a legacy migration may create, or None."""
    accounts = google_accounts(db=db)
    assert len(accounts) <= 1
    return accounts[0] if accounts else None


def _seed_legacy(db: sqlite3.Connection) -> None:
    key_store.set_managed_value(key_store.GOOGLE_CLIENT_ID, "client-id", db=db)
    key_store.set_managed_value(key_store.GOOGLE_CLIENT_SECRET, "client-secret", db=db)
    key_store.set_managed_value(key_store.GOOGLE_REFRESH_TOKEN, "refresh-token", db=db)
    set_setting("google_account", {"email": ACCOUNT, "status": "connected"}, db=db)
    set_setting("gmail_poll_cursor", CURSOR, db=db)


def test_v126_registers_connected_accounts_step() -> None:
    assert SCHEMA_VERSION == 127
    assert MIGRATIONS[126] is migrate_126_to_127
    assert 127 not in MIGRATIONS


def test_runner_migrates_legacy_google_once_and_removes_the_singleton_path() -> None:
    with closing(_dev_126()) as db:
        _seed_legacy(db)
        db.execute("PRAGMA user_version=126")
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 126, 127)
        account = google_account(db=db)
        assert account is not None
        assert account["address"] == ACCOUNT
        assert account["reach"] == corsia_only_reach().as_dict()
        assert account["cursor"] == CURSOR
        assert resolve_google_credentials(str(account["id"]), db=db) == {
            "client_id": "client-id", "client_secret": "client-secret", "refresh_token": "refresh-token",
        }
        assert get_setting("google_account", db=db) is None
        assert get_setting("gmail_poll_cursor", db=db) is None
        assert key_store.resolve_names(key_store.GOOGLE_ENTRIES, db=db) == {}
        assert db.execute("SELECT account_id FROM connected_account_migration_journal").fetchone()["account_id"] == account["id"]
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_incomplete_legacy_credentials_become_needs_sign_in_without_blocking_startup() -> None:
    with closing(_dev_126()) as db:
        _seed_legacy(db)
        db.execute("DELETE FROM key_store_entries WHERE name=?", (key_store.GOOGLE_REFRESH_TOKEN,))
        db.execute("PRAGMA user_version=126")
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.to_version) == (True, 127)
        account = google_account(db=db)
        assert account is not None
        assert account["address"] == ACCOUNT
        assert account["cursor"] == CURSOR
        assert account["state"] == "needs_sign_in"
        assert resolve_google_credentials(str(account["id"]), db=db) is None
        assert get_setting("google_account", db=db) is None
        assert key_store.resolve_names(key_store.GOOGLE_ENTRIES, db=db) == {}
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_addressless_legacy_data_is_retired_and_journaled_without_an_account() -> None:
    with closing(_dev_126()) as db:
        _seed_legacy(db)
        set_setting("google_account", {"status": "connected"}, db=db)
        db.execute("PRAGMA user_version=126")
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.to_version) == (True, 127)
        assert google_account(db=db) is None
        assert get_setting("google_account", db=db) is None
        assert get_setting("gmail_poll_cursor", db=db) is None
        assert key_store.resolve_names(key_store.GOOGLE_ENTRIES, db=db) == {}
        assert db.execute("SELECT account_id FROM connected_account_migration_journal").fetchone()["account_id"] is None
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_failed_bundle_write_keeps_legacy_credentials_and_settings_for_retry(monkeypatch) -> None:
    with closing(_dev_126()) as db:
        _seed_legacy(db)
        db.execute("PRAGMA user_version=126")
        db.commit()

        monkeypatch.setattr(key_store_values, "put", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(MigrationError):
            migrate_to_current(db, SCHEMA_VERSION)

        assert get_setting("google_account", db=db) == {"email": ACCOUNT, "status": "connected"}
        assert get_setting("gmail_poll_cursor", db=db) == CURSOR
        assert set(key_store.resolve_names(key_store.GOOGLE_ENTRIES, db=db)) == set(key_store.GOOGLE_ENTRIES)
        assert db.execute("SELECT COUNT(*) FROM connected_accounts").fetchone()[0] == 0


def _screen(db: sqlite3.Connection, account: str, message_id: str) -> None:
    db.execute(
        "INSERT INTO email_screens(account,message_id,status,screened_at) VALUES (?,?,'unavailable','t')",
        (account, message_id),
    )


def test_stored_screens_are_rekeyed_to_the_new_account_and_strays_dropped() -> None:
    with closing(_dev_126()) as db:
        _seed_legacy(db)
        _screen(db, ACCOUNT.upper(), "m1")  # Address case differs from the setting's.
        _screen(db, "someone-else@example.test", "m2")
        db.execute("PRAGMA user_version=126")
        db.commit()

        migrate_to_current(db, SCHEMA_VERSION)

        account = google_account(db=db)
        assert account is not None
        rows = db.execute("SELECT account, message_id FROM email_screens ORDER BY message_id").fetchall()
        assert [tuple(row) for row in rows] == [(account["id"], "m1")]


def test_screens_without_a_legacy_account_are_dropped() -> None:
    with closing(_dev_126()) as db:
        _screen(db, ACCOUNT, "m1")
        db.execute("PRAGMA user_version=126")
        db.commit()

        migrate_to_current(db, SCHEMA_VERSION)

        assert db.execute("SELECT COUNT(*) FROM email_screens").fetchone()[0] == 0


def test_dev_126_upgrades_to_the_complete_connected_accounts_schema() -> None:
    with closing(_dev_126()) as db:
        migrate_to_current(db, SCHEMA_VERSION)

        expected_columns = {
            "connected_accounts": {
                "id", "provider", "provider_subject", "address", "capabilities_json", "state",
                "reach_mode", "outside_projects", "credential_key", "cursor_json", "created_by_kind",
                "created_by_id", "signed_in_by_kind", "signed_in_by_id", "created_at", "updated_at",
            },
            "connected_account_reach": {"account_id", "project_id"},
            "connected_account_audit": {
                "id", "created_at", "account_id", "actor_kind", "actor_id", "action", "revocation_cause",
            },
            "connected_account_migration_journal": {"migration_key", "completed_at", "account_id"},
        }
        for table, columns in expected_columns.items():
            actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            assert actual == columns


def test_account_history_survives_remove_with_foreign_keys_enabled() -> None:
    """Remove deletes the account row; its audit and migration records must outlive it."""
    from connections.google import account_actions

    with closing(_dev_126()) as db:
        _seed_legacy(db)
        migrate_to_current(db, SCHEMA_VERSION)
        account = google_account(db=db)
        assert account is not None
        account_id = str(account["id"])

        db.execute("PRAGMA foreign_keys=ON")
        account_actions._remove_locally(account_id, ("person", "owner"), db=db)

        assert db.execute("SELECT COUNT(*) FROM connected_accounts WHERE id=?", (account_id,)).fetchone()[0] == 0
        assert db.execute(
            "SELECT action FROM connected_account_audit WHERE account_id=? ORDER BY id", (account_id,),
        ).fetchall()[-1][0] == "remove"
        assert db.execute(
            "SELECT account_id FROM connected_account_migration_journal WHERE migration_key=?", ("google-singleton-v1",),
        ).fetchone()[0] == account_id
        for table in ("connected_account_audit", "connected_account_migration_journal"):
            assert db.execute(f"PRAGMA foreign_key_list({table})").fetchall() == []


def test_fresh_install_has_final_history_tables_and_audit_causes() -> None:
    with closing(_fresh()) as db:
        for table in ("connected_account_audit", "connected_account_migration_journal"):
            assert db.execute(f"PRAGMA foreign_key_list({table})").fetchall() == []
        row = ("t", "account", "person", "owner", "remove")
        db.execute(
            "INSERT INTO connected_account_audit(created_at, account_id, actor_kind, actor_id, action, revocation_cause) "
            "VALUES (?,?,?,?,?,?)", (*row, "not_signed_in"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO connected_account_audit(created_at, account_id, actor_kind, actor_id, action, revocation_cause) "
                "VALUES (?,?,?,?,?,?)", (*row, "manual_only"),
            )


LINKED_HISTORY_SQL = """
    CREATE TABLE connected_account_audit (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        account_id TEXT NOT NULL REFERENCES connected_accounts(id) ON DELETE CASCADE,
        actor_kind TEXT NOT NULL CHECK(actor_kind IN ('person', 'agent', 'system')),
        actor_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('connect', 'sign_in', 'sign_out', 'remove')),
        revocation_cause TEXT CHECK(revocation_cause IS NULL OR revocation_cause IN (
            'revoked', 'provider_unreachable', 'provider_timeout',
            'provider_rejected', 'provider_unexpected', 'manual_only'
        ))
    );
    CREATE INDEX idx_connected_account_audit_account ON connected_account_audit(account_id, id);
    CREATE TABLE connected_account_migration_journal (
        migration_key TEXT PRIMARY KEY,
        completed_at TEXT NOT NULL,
        account_id TEXT REFERENCES connected_accounts(id)
    );
"""


def test_transient_train_history_is_repaired_without_losing_rows() -> None:
    from connections.google import account_actions

    with closing(_dev_126()) as db:
        _seed_legacy(db)
        migrate_to_current(db, SCHEMA_VERSION)
        account = google_account(db=db)
        assert account is not None
        account_id = str(account["id"])

        db.execute("DROP TABLE connected_account_audit")
        db.execute("DROP TABLE connected_account_migration_journal")
        db.executescript(LINKED_HISTORY_SQL)
        db.execute(
            "INSERT INTO connected_account_audit(created_at, account_id, actor_kind, actor_id, action, revocation_cause) "
            "VALUES ('t0', ?, 'system', 'test', 'connect', NULL)", (account_id,),
        )
        db.execute(
            "INSERT INTO connected_account_audit(created_at, account_id, actor_kind, actor_id, action, revocation_cause) "
            "VALUES ('t1', ?, 'person', 'owner', 'remove', 'manual_only')", (account_id,),
        )
        db.execute(
            "INSERT INTO connected_account_migration_journal(migration_key, completed_at, account_id) VALUES (?,?,?)",
            ("google-singleton-v1", "t0", account_id),
        )
        db.execute("PRAGMA user_version=126")
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 126, 127)
        assert [row[0] for row in db.execute("SELECT revocation_cause FROM connected_account_audit ORDER BY id")] == [
            None, "not_signed_in",
        ]
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_connected_account_audit_account'",
        ).fetchone()
        for table in ("connected_account_audit", "connected_account_migration_journal"):
            assert db.execute(f"PRAGMA foreign_key_list({table})").fetchall() == []

        db.execute("PRAGMA foreign_keys=ON")
        account_actions._remove_locally(account_id, ("person", "owner"), db=db)
        assert db.execute(
            "SELECT COUNT(*) FROM connected_account_audit WHERE account_id=?", (account_id,),
        ).fetchone()[0] == 3
        assert db.execute(
            "SELECT account_id FROM connected_account_migration_journal WHERE migration_key=?", ("google-singleton-v1",),
        ).fetchone()[0] == account_id
