"""Schema 125 -> 126: connected accounts and legacy Google migration."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from connections.connected_accounts import corsia_only_reach, google_account, resolve_google_credentials
from infra import key_store, key_store_values
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current
from infra.migration_125_to_126_connected_accounts import migrate_125_to_126
from infra.schema import SCHEMA_VERSION
from infra.settings import get_setting, set_setting


ACCOUNT = "person@example.test"
CURSOR = {"account": ACCOUNT, "floor_at": "old-floor", "history_id": "old-history", "last_success_at": "old-success"}


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    return db


def _seed_legacy(db: sqlite3.Connection) -> None:
    key_store.set_managed_value(key_store.GOOGLE_CLIENT_ID, "client-id", db=db)
    key_store.set_managed_value(key_store.GOOGLE_CLIENT_SECRET, "client-secret", db=db)
    key_store.set_managed_value(key_store.GOOGLE_REFRESH_TOKEN, "refresh-token", db=db)
    set_setting("google_account", {"email": ACCOUNT, "status": "connected"}, db=db)
    set_setting("gmail_poll_cursor", CURSOR, db=db)


def test_v125_registers_connected_accounts_step() -> None:
    assert SCHEMA_VERSION == 126
    assert MIGRATIONS[125] is migrate_125_to_126


def test_runner_migrates_legacy_google_once_and_removes_the_singleton_path() -> None:
    with closing(_fresh()) as db:
        _seed_legacy(db)
        db.execute("PRAGMA user_version=125")
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 125, 126)
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


def test_failed_bundle_write_keeps_legacy_credentials_and_settings_for_retry(monkeypatch) -> None:
    with closing(_fresh()) as db:
        _seed_legacy(db)
        db.execute("PRAGMA user_version=125")
        db.commit()

        monkeypatch.setattr(key_store_values, "put", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(MigrationError):
            migrate_to_current(db, SCHEMA_VERSION)

        assert get_setting("google_account", db=db) == {"email": ACCOUNT, "status": "connected"}
        assert get_setting("gmail_poll_cursor", db=db) == CURSOR
        assert set(key_store.resolve_names(key_store.GOOGLE_ENTRIES, db=db)) == set(key_store.GOOGLE_ENTRIES)
        assert db.execute("SELECT COUNT(*) FROM connected_accounts").fetchone()[0] == 0
