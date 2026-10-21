"""Schema 131 -> 132: Microsoft account row metadata and fixed audit causes."""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing

from connections.account_state import REGISTRATION_MISCONFIGURED, REVOCATION_CAUSES, STATE_CAUSES
from infra.connected_accounts_schema import AUDIT_TABLE_SQL, CONNECTED_ACCOUNTS_SCHEMA_SQL, _sql_values as schema_sql_values
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_131_to_132_microsoft_accounts import _AUDIT_V132_SQL, _CONNECTED_ACCOUNTS_V132_SQL, migrate_131_to_132
from infra.schema import SCHEMA_VERSION


def _v131() -> sqlite3.Connection:
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
    db.execute("PRAGMA user_version=131")
    db.commit()
    return db


def _literal_check_values(sql: str, column: str) -> frozenset[str]:
    match = re.search(rf"{column} TEXT CHECK\({column} IS NULL OR {column} IN \((.*?)\)\)", sql, re.DOTALL)
    assert match is not None
    return frozenset(re.findall(r"'([^']+)'", match.group(1)))


def test_account_cause_checks_match_the_canonical_cause_sets() -> None:
    assert REGISTRATION_MISCONFIGURED in STATE_CAUSES
    assert schema_sql_values(STATE_CAUSES) in CONNECTED_ACCOUNTS_SCHEMA_SQL
    assert schema_sql_values(REVOCATION_CAUSES) in AUDIT_TABLE_SQL
    assert REVOCATION_CAUSES.isdisjoint(STATE_CAUSES)
    assert _literal_check_values(_CONNECTED_ACCOUNTS_V132_SQL, "state_cause") == STATE_CAUSES
    assert _literal_check_values(_AUDIT_V132_SQL, "revocation_cause") == REVOCATION_CAUSES


def test_v131_registers_microsoft_account_step() -> None:
    assert SCHEMA_VERSION == 135
    assert MIGRATIONS[131] is migrate_131_to_132


def test_migration_adds_metadata_and_accepts_manual_only_audit() -> None:
    with closing(_v131()) as db:
        db.execute(
            "INSERT INTO connected_account_audit(created_at,account_id,actor_kind,actor_id,action,revocation_cause) "
            "VALUES ('before','account','person','person','sign_out','not_signed_in')"
        )
        db.commit()
        result = migrate_to_current(db, 132)
        assert (result.migrated, result.from_version, result.to_version) == (True, 131, 132)
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
        assert not migrate_to_current(db, 132).migrated


def _snapshot(db: sqlite3.Connection, table: str, columns: str = "*") -> list[tuple]:
    return [tuple(row) for row in db.execute(f"SELECT {columns} FROM {table} ORDER BY 1,2")]


def test_existing_google_accounts_survive_byte_for_byte_in_every_reach_mode() -> None:
    from infra import key_store

    with closing(_v131()) as db:
        db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('project-a','a','t','t')")
        for account_id, mode, outside in (("g-all", "all", 0), ("g-selected", "selected", 1), ("g-corsia", "corsia", 0)):
            key = f"connected-account--google--{account_id}"
            db.execute(
                "INSERT INTO connected_accounts(id,provider,provider_subject,address,capabilities_json,state,reach_mode,"
                "outside_projects,credential_key,cursor_json,created_by_kind,created_by_id,signed_in_by_kind,"
                "signed_in_by_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, "google", f"subject-{account_id}", f"{account_id}@example.test", '["mail","calendar","files"]',
                 "connected", mode, outside, key, '{"history_id":"7"}', "person", "person", "person", "person",
                 "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"),
            )
        db.execute("INSERT INTO connected_account_reach(account_id,project_id) VALUES ('g-selected','project-a')")
        db.commit()
        for account_id in ("g-all", "g-selected", "g-corsia"):
            key_store.set_managed_value(
                f"connected-account--google--{account_id}", '{"client_id":"c","client_secret":"s","refresh_token":"r"}', db=db,
            )
        columns = ",".join(row["name"] for row in db.execute("PRAGMA table_info(connected_accounts)"))
        before = (
            _snapshot(db, "connected_accounts", columns), _snapshot(db, "connected_account_reach"),
            _snapshot(db, "key_store_entries"),
        )

        assert migrate_to_current(db, 132).migrated

        after = (
            _snapshot(db, "connected_accounts", columns), _snapshot(db, "connected_account_reach"),
            _snapshot(db, "key_store_entries"),
        )
        assert after == before
        assert len(before[0]) == 3 and len(before[1]) == 1 and len(before[2]) >= 3
        assert _snapshot(db, "connected_accounts", "account_kind,state_cause") == [(None, None)] * 3
