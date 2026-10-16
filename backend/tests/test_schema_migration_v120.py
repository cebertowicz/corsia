"""Schema 119 -> 120: email screen results are additive and constrained."""

from __future__ import annotations

import sqlite3

import pytest

from infra.database_migrations import MIGRATIONS
from infra.migration_119_to_120_email_screens import migrate_119_to_120
from infra.schema import SCHEMA_VERSION


def _columns(db: sqlite3.Connection) -> list[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(email_screens)")]


def test_v120_registers_the_email_screens_step() -> None:
    assert SCHEMA_VERSION == 121
    assert MIGRATIONS[119] is migrate_119_to_120


def test_email_screens_step_creates_the_expected_table() -> None:
    with sqlite3.connect(":memory:") as db:
        migrate_119_to_120(db)
        assert _columns(db) == [
            "account", "message_id", "status", "kind", "needs_person", "injection",
            "injection_rule", "model_id", "screened_at", "attempts",
        ]
        db.execute(
            "INSERT INTO email_screens (account,message_id,status,screened_at) "
            "VALUES ('account','message','screened','now')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO email_screens (account,message_id,status,screened_at) "
                "VALUES ('other','message','unknown','now')"
            )
