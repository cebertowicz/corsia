"""Schema v114 separates a CLI harness switch from source-check status."""

from __future__ import annotations

import sqlite3

from infra.database_migrations import MIGRATIONS
from infra.migration_113_to_114_harness_enabled import migrate_113_to_114
from infra.schema import SCHEMA_VERSION


def test_registered_as_the_current_step():
    assert SCHEMA_VERSION == 114
    assert MIGRATIONS[113] is migrate_113_to_114


def test_adds_enabled_without_rewriting_observed_status():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE connections (provider TEXT PRIMARY KEY, status TEXT NOT NULL)")
    db.execute("INSERT INTO connections VALUES ('codex', 'needs_auth')")

    migrate_113_to_114(db)
    migrate_113_to_114(db)

    assert db.execute("SELECT status, enabled FROM connections").fetchone() == ("needs_auth", 1)
