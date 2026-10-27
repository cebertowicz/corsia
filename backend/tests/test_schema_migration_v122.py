"""Schema 121 -> 122: email screen results are additive and constrained."""

from __future__ import annotations

import sqlite3

import pytest

from infra.database_migrations import MIGRATIONS
from infra.migration_121_to_122_email_screens import migrate_121_to_122
from infra.schema import SCHEMA_VERSION


def _columns(db: sqlite3.Connection) -> list[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(email_screens)")]


def test_v121_registers_the_email_screens_step() -> None:
    assert SCHEMA_VERSION == 151
    assert MIGRATIONS[121] is migrate_121_to_122


def test_email_screens_step_creates_the_expected_table() -> None:
    with sqlite3.connect(":memory:") as db:
        migrate_121_to_122(db)
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
