"""Schema 148 -> 149: totals of admitted install usage summaries and the delivery ids already counted."""
from __future__ import annotations

import sqlite3

import pytest

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current
from infra.migration_148_to_149_usage_totals import migrate_148_to_149
from infra.schema import SCHEMA_VERSION


def _legacy_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    db.execute("DROP TABLE usage_install_totals")
    db.execute("DROP TABLE usage_admitted_deliveries")
    db.execute("PRAGMA user_version=148")
    return db


def test_v149_fresh_schema_has_the_usage_tables_empty() -> None:
    assert SCHEMA_VERSION == 149
    assert MIGRATIONS[148] is migrate_148_to_149
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    assert db.execute("SELECT count(*) FROM usage_install_totals").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM usage_admitted_deliveries").fetchone()[0] == 0


def test_v149_upgrade_creates_the_tables_and_is_a_no_op_after() -> None:
    db = _legacy_db()
    try:
        assert migrate_to_current(db, SCHEMA_VERSION).to_version == 149
        assert {row[1] for row in db.execute("PRAGMA table_info(usage_install_totals)")} == {"week", "release_tag", "os", "chip", "install_weeks"}
        assert {row[1] for row in db.execute("PRAGMA table_info(usage_admitted_deliveries)")} == {"delivery_id", "week"}
        assert migrate_to_current(db, SCHEMA_VERSION).migrated is False
    finally:
        db.close()


def test_v149_failure_after_the_step_rolls_back_the_tables(monkeypatch) -> None:
    def failing_step(db):
        migrate_148_to_149(db)
        raise RuntimeError("synthetic failure after the v149 step")

    monkeypatch.setitem(MIGRATIONS, 148, failing_step)
    db = _legacy_db()
    try:
        db.commit()
        before = list(db.iterdump())
        with pytest.raises(MigrationError, match="synthetic failure after the v149 step"):
            migrate_to_current(db, SCHEMA_VERSION)
        assert not db.in_transaction
        assert db.execute("PRAGMA user_version").fetchone()[0] == 148
        assert list(db.iterdump()) == before
    finally:
        db.close()
