"""Schema 147 -> 148: totals of admitted install usage summaries."""
from __future__ import annotations

import sqlite3

import pytest

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current
from infra.migration_147_to_148_usage_totals import migrate_147_to_148
from infra.schema import SCHEMA_VERSION


def _legacy_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    db.execute("DROP TABLE usage_install_totals")
    db.execute("PRAGMA user_version=147")
    return db


def test_v148_fresh_schema_has_the_totals_table_empty() -> None:
    assert SCHEMA_VERSION == 148
    assert MIGRATIONS[147] is migrate_147_to_148
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    assert db.execute("SELECT count(*) FROM usage_install_totals").fetchone()[0] == 0


def test_v148_upgrade_creates_the_table_and_is_a_no_op_after() -> None:
    db = _legacy_db()
    try:
        assert migrate_to_current(db, SCHEMA_VERSION).to_version == 148
        assert {row[1] for row in db.execute("PRAGMA table_info(usage_install_totals)")} == {"week", "release_tag", "os", "chip", "install_weeks"}
        assert migrate_to_current(db, SCHEMA_VERSION).migrated is False
    finally:
        db.close()


def test_v148_failure_after_the_step_rolls_back_the_table(monkeypatch) -> None:
    def failing_step(db):
        migrate_147_to_148(db)
        raise RuntimeError("synthetic failure after the v148 step")

    monkeypatch.setitem(MIGRATIONS, 147, failing_step)
    db = _legacy_db()
    try:
        db.commit()
        before = list(db.iterdump())
        with pytest.raises(MigrationError, match="synthetic failure after the v148 step"):
            migrate_to_current(db, SCHEMA_VERSION)
        assert not db.in_transaction
        assert db.execute("PRAGMA user_version").fetchone()[0] == 147
        assert list(db.iterdump()) == before
    finally:
        db.close()
