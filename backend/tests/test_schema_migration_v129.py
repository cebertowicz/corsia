"""Schema 128 -> 129: connected accounts and legacy Google migration."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from connections.connected_accounts import GOOGLE_PROVIDER, resolve_credentials
from google_account_helpers import ONLY_CORSIA_REACH
from infra import key_store, key_store_values
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current
from infra.migration_128_to_129_connected_accounts import migrate_128_to_129
from infra.schema import SCHEMA_VERSION
from infra.settings import get_setting, set_setting


ACCOUNT = "person@example.test"
CURSOR = {"account": ACCOUNT, "floor_at": "old-floor", "history_id": "old-history", "last_success_at": "old-success"}


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    return db


def _dev_128() -> sqlite3.Connection:
    """Reconstruct dev schema 128, including the measure registry, in memory."""
    db = _fresh()
    db.execute("DROP TABLE connected_account_reach")
    db.execute("DROP TABLE connected_account_audit")

    db.execute("DROP TABLE connected_accounts")
    db.execute("PRAGMA user_version=128")
    db.commit()
    return db


def google_account(*, db):  # type: ignore[no-untyped-def]
    """The single account a legacy migration may create, or None, read from the frozen v129 table.

    Live account code reads the current schema, so it is never pointed at a v129 table.
    """
    rows = db.execute(
        "SELECT id,address,state,reach_mode,outside_projects,cursor_json FROM connected_accounts WHERE provider=?",
        (GOOGLE_PROVIDER,),
    ).fetchall()
    assert len(rows) <= 1
    if not rows:
        return None
    row = rows[0]
    reach = [r["project_id"] for r in db.execute("SELECT project_id FROM connected_account_reach WHERE account_id=?", (row["id"],))]
    return {
        "id": row["id"], "address": row["address"], "state": row["state"],
        "reach": {"mode": row["reach_mode"], "project_ids": sorted(reach), "outside_projects": bool(row["outside_projects"])},
        "cursor": json.loads(row["cursor_json"]) if row["cursor_json"] else None,
    }


def _seed_legacy(db: sqlite3.Connection) -> None:
    key_store.set_managed_value(key_store.GOOGLE_CLIENT_ID, "client-id", db=db)
    key_store.set_managed_value(key_store.GOOGLE_CLIENT_SECRET, "client-secret", db=db)
    key_store.set_managed_value(key_store.GOOGLE_REFRESH_TOKEN, "refresh-token", db=db)
    set_setting("google_account", {"email": ACCOUNT, "status": "connected"}, db=db)
    set_setting("gmail_poll_cursor", CURSOR, db=db)


def test_v128_registers_connected_accounts_step() -> None:
    assert SCHEMA_VERSION == 151
    assert MIGRATIONS[128] is migrate_128_to_129
    assert 129 in MIGRATIONS


def test_runner_migrates_legacy_google_once_and_removes_the_singleton_path() -> None:
    with closing(_dev_128()) as db:
        _seed_legacy(db)
        db.execute("PRAGMA user_version=128")
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 128, 151)
        account = google_account(db=db)
        assert account is not None
        assert account["address"] == ACCOUNT
        assert account["reach"] == ONLY_CORSIA_REACH
        assert account["cursor"] == CURSOR
        assert resolve_credentials(str(account["id"]), db=db) == {
            "client_id": "client-id", "client_secret": "client-secret", "refresh_token": "refresh-token",
        }
        assert get_setting("google_account", db=db) is None
        assert get_setting("gmail_poll_cursor", db=db) is None
        assert key_store.resolve_names(key_store.GOOGLE_ENTRIES, db=db) == {}

        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_migration_rekeys_owned_threads_and_binds_open_google_drafts() -> None:
    with closing(_dev_128()) as db:
        _seed_legacy(db)
        db.execute(
            "INSERT INTO threads(id,user_id,created_at,updated_at) VALUES (?,?,?,?)",
            ("owner-thread", "owner", "t", "t"),
        )
        db.execute(
            "INSERT INTO artifact_identities(id,integration,type,external_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("email-artifact", "gmail", "email-thread", "raw-thread", "t", "t"),
        )
        db.execute(
            "INSERT INTO artifact_registrations(id,artifact_id,parent_thread_id,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("email-owner", "email-artifact", "owner-thread", "t", "t"),
        )
        db.execute(
            "INSERT INTO connection_rules(id,connector,fact,value,destination_thread_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("gmail-thread-rule", "gmail", "thread_id", "raw-thread", "owner-thread", "t", "t"),
        )
        db.execute(
            "INSERT INTO connection_rules(id,connector,fact,value,destination_thread_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("gmail-label-rule", "gmail", "label", "inbox", "owner-thread", "t", "t"),
        )
        for kind in ("email", "invite"):
            db.execute(
                "INSERT INTO action_drafts(id,thread_id,kind,path,target_json,recorded_revision,recorded_at,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"{kind}-draft", "owner-thread", kind, f"{kind}.md", "{}", "r1", "t", "t"),
            )
        db.execute("PRAGMA user_version=128")
        db.commit()

        migrate_to_current(db, 129)

        account = google_account(db=db)
        assert account is not None
        account_id = str(account["id"])
        assert db.execute(
            "SELECT external_id FROM artifact_identities WHERE id='email-artifact'",
        ).fetchone()[0] == f"google:{account_id}:thread:raw-thread"
        assert db.execute(
            "SELECT parent_thread_id FROM artifact_registrations WHERE artifact_id='email-artifact'",
        ).fetchone()[0] == "owner-thread"
        assert db.execute(
            "SELECT value FROM connection_rules WHERE id='gmail-thread-rule'",
        ).fetchone()[0] == f"google:{account_id}:thread:raw-thread"
        assert db.execute(
            "SELECT value FROM connection_rules WHERE id='gmail-label-rule'",
        ).fetchone()[0] == "inbox"
        assert [row[0] for row in db.execute(
            "SELECT target_json FROM action_drafts ORDER BY id",
        )] == [
            f'{{"account_id":"{account_id}"}}', f'{{"account_id":"{account_id}"}}',
        ]


def test_incomplete_legacy_credentials_become_needs_sign_in_without_blocking_startup() -> None:
    with closing(_dev_128()) as db:
        _seed_legacy(db)
        db.execute("DELETE FROM key_store_entries WHERE name=?", (key_store.GOOGLE_REFRESH_TOKEN,))
        db.execute("PRAGMA user_version=128")
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.to_version) == (True, 151)
        account = google_account(db=db)
        assert account is not None
        assert account["address"] == ACCOUNT
        assert account["cursor"] == CURSOR
        assert account["state"] == "needs_sign_in"
        assert resolve_credentials(str(account["id"]), db=db) is None
        assert get_setting("google_account", db=db) is None
        assert key_store.resolve_names(key_store.GOOGLE_ENTRIES, db=db) == {}
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_addressless_legacy_data_is_retired_without_an_account() -> None:
    with closing(_dev_128()) as db:
        _seed_legacy(db)
        set_setting("google_account", {"status": "connected"}, db=db)
        db.execute("PRAGMA user_version=128")
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.to_version) == (True, 151)
        assert google_account(db=db) is None
        assert get_setting("google_account", db=db) is None
        assert get_setting("gmail_poll_cursor", db=db) is None
        assert key_store.resolve_names(key_store.GOOGLE_ENTRIES, db=db) == {}

        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_failed_bundle_write_keeps_legacy_credentials_and_settings_for_retry(monkeypatch) -> None:
    with closing(_dev_128()) as db:
        _seed_legacy(db)
        db.execute("PRAGMA user_version=128")
        db.commit()

        monkeypatch.setattr(key_store_values, "put", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(MigrationError):
            migrate_to_current(db, 129)

        assert get_setting("google_account", db=db) == {"email": ACCOUNT, "status": "connected"}
        assert get_setting("gmail_poll_cursor", db=db) == CURSOR
        assert set(key_store.resolve_names(key_store.GOOGLE_ENTRIES, db=db)) == set(key_store.GOOGLE_ENTRIES)
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='connected_accounts'",
        ).fetchone() is None


def test_failed_multistep_ladder_rolls_back_earlier_schema_changes_and_retries(monkeypatch) -> None:
    with closing(_dev_128()) as db:
        _seed_legacy(db)
        db.execute("DROP INDEX idx_doc_comments_user_created")
        db.execute("PRAGMA user_version=126")
        db.commit()

        monkeypatch.setattr(key_store_values, "put", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(MigrationError):
            migrate_to_current(db, 129)

        assert db.execute("PRAGMA user_version").fetchone()[0] == 126
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_doc_comments_user_created'",
        ).fetchone() is None
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='connected_accounts'",
        ).fetchone() is None

        monkeypatch.undo()
        assert migrate_to_current(db, SCHEMA_VERSION).to_version == 151


def _screen(db: sqlite3.Connection, account: str, message_id: str) -> None:
    db.execute(
        "INSERT INTO email_screens(account,message_id,status,screened_at) VALUES (?,?,'unavailable','t')",
        (account, message_id),
    )


def test_stored_screens_are_rekeyed_to_the_new_account_and_strays_dropped() -> None:
    with closing(_dev_128()) as db:
        _seed_legacy(db)
        _screen(db, ACCOUNT.upper(), "m1")  # Address case differs from the setting's.
        _screen(db, "someone-else@example.test", "m2")
        db.execute("PRAGMA user_version=128")
        db.commit()

        migrate_to_current(db, 129)

        account = google_account(db=db)
        assert account is not None
        rows = db.execute("SELECT account, message_id FROM email_screens ORDER BY message_id").fetchall()
        assert [tuple(row) for row in rows] == [(account["id"], "m1")]


def test_screens_without_a_legacy_account_are_dropped() -> None:
    with closing(_dev_128()) as db:
        _screen(db, ACCOUNT, "m1")
        db.execute("PRAGMA user_version=128")
        db.commit()

        migrate_to_current(db, 129)

        assert db.execute("SELECT COUNT(*) FROM email_screens").fetchone()[0] == 0


def test_dev_128_upgrades_to_the_complete_connected_accounts_schema() -> None:
    with closing(_dev_128()) as db:
        migrate_to_current(db, 129)

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

        }
        for table, columns in expected_columns.items():
            actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            assert actual == columns


def test_account_history_survives_remove_with_foreign_keys_enabled() -> None:
    """Remove deletes the account row; its audit record must outlive it."""
    from connections import account_actions

    with closing(_dev_128()) as db:
        _seed_legacy(db)
        migrate_to_current(db, 129)
        account = google_account(db=db)
        assert account is not None
        account_id = str(account["id"])

        migrate_to_current(db, SCHEMA_VERSION)  # Remove runs on the current schema.
        db.execute("PRAGMA foreign_keys=ON")
        account_actions._remove_locally(account_id, ("person", "owner"), db=db)

        assert db.execute("SELECT COUNT(*) FROM connected_accounts WHERE id=?", (account_id,)).fetchone()[0] == 0
        assert db.execute(
            "SELECT action FROM connected_account_audit WHERE account_id=? ORDER BY id", (account_id,),
        ).fetchall()[-1][0] == "remove"
        assert db.execute("PRAGMA foreign_key_list(connected_account_audit)").fetchall() == []


def test_fresh_install_has_final_history_tables_and_audit_causes() -> None:
    with closing(_fresh()) as db:
        assert db.execute("PRAGMA foreign_key_list(connected_account_audit)").fetchall() == []
        row = ("t", "account", "person", "owner", "remove")
        db.execute(
            "INSERT INTO connected_account_audit(created_at, account_id, actor_kind, actor_id, action, revocation_cause) "
            "VALUES (?,?,?,?,?,?)", (*row, "not_signed_in"),
        )
        db.execute(
            "INSERT INTO connected_account_audit(created_at, account_id, actor_kind, actor_id, action, revocation_cause) "
            "VALUES (?,?,?,?,?,?)", (*row, "manual_only"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO connected_account_audit(created_at, account_id, actor_kind, actor_id, action, revocation_cause) "
                "VALUES (?,?,?,?,?,?)", (*row, "provider_response_text"),
            )


# The transient train-only history schema never reached a development build; migration 128→129 does not repair it.
