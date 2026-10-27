"""Schema 144 -> 145: a connected account's non-secret server settings, for mail hosts."""

from __future__ import annotations

import sqlite3

import pytest

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_144_to_145_mail_host_endpoints import migrate_144_to_145
from infra.schema import SCHEMA_VERSION


def _columns(db: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in db.execute("PRAGMA table_info(connected_accounts)")}


def _v144() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    db.execute("ALTER TABLE connected_accounts DROP COLUMN endpoints_json")
    db.execute("PRAGMA user_version=144")
    db.commit()
    return db


def _insert_google(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO connected_accounts (id, provider, provider_subject, address, capabilities_json, state, "
        "reach_mode, outside_projects, credential_key, cursor_json, created_by_kind, created_by_id, created_at, updated_at) "
        "VALUES ('a1','google','sub','a@example.com','[\"mail\"]','connected','all',0,'k','{}','person','p','t','t')"
    )


def test_step_is_registered_at_the_current_version() -> None:
    assert SCHEMA_VERSION == 150
    assert MIGRATIONS[144] is migrate_144_to_145


def test_a_fresh_schema_has_the_column() -> None:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    assert "endpoints_json" in _columns(db)


def test_the_step_adds_a_null_column_and_keeps_every_row() -> None:
    db = _v144()
    _insert_google(db)
    db.commit()
    assert "endpoints_json" not in _columns(db)
    migrate_to_current(db, SCHEMA_VERSION)
    assert "endpoints_json" in _columns(db)
    row = db.execute("SELECT address, endpoints_json FROM connected_accounts").fetchone()
    assert (row["address"], row["endpoints_json"]) == ("a@example.com", None)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_the_step_is_safe_to_run_twice() -> None:
    db = _v144()
    migrate_144_to_145(db)
    migrate_144_to_145(db)
    assert "endpoints_json" in _columns(db)


def test_the_column_accepts_json_and_refuses_anything_else() -> None:
    db = _v144()
    migrate_144_to_145(db)
    _insert_google(db)
    db.execute("UPDATE connected_accounts SET endpoints_json='{\"imap\":{}}'")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE connected_accounts SET endpoints_json='not json'")
