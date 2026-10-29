"""Schema 118 -> 119 creates lesson placement-check observations."""
from __future__ import annotations

import sqlite3

from infra.database_migrations import MIGRATIONS
from infra.migration_118_to_119_lesson_placement_checks import migrate_118_to_119
from infra.schema import SCHEMA_VERSION


def test_registers_the_lesson_placement_check_migration():
    assert SCHEMA_VERSION == 162
    assert MIGRATIONS[118] is migrate_118_to_119


def test_creates_idempotent_check_observations_table():
    db = sqlite3.connect(":memory:")

    migrate_118_to_119(db)
    migrate_118_to_119(db)
    db.execute(
        """INSERT INTO lesson_placement_checks VALUES
        ('thread', 0, 1, 1, 'incident-details', 'date', NULL, 'now')"""
    )

    assert db.execute("SELECT count(*) FROM lesson_placement_checks").fetchone()[0] == 1
