"""Schema 132 -> 133: correction-learning subject uniqueness."""
from __future__ import annotations

import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_132_to_133_correction_learning import migrate_132_to_133
from infra.schema import SCHEMA_VERSION


def test_v132_registers_correction_learning_step() -> None:
    assert SCHEMA_VERSION >= 133
    assert MIGRATIONS[132] is migrate_132_to_133


def test_migration_adds_per_subject_learning_indexes() -> None:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    db.execute("DROP INDEX idx_learning_loop_triage_subject")
    db.execute("DROP INDEX idx_learning_loop_outcome_subject")
    db.execute("PRAGMA user_version=132")
    assert migrate_to_current(db, 133).migrated
    db.execute("INSERT INTO outcome_events VALUES ('1','a','learning_loop.correction_triage','agent','agent_recorded','t','t','subject',NULL,NULL,NULL,NULL,'{}',NULL)")
    try:
        db.execute("INSERT INTO outcome_events VALUES ('2','b','learning_loop.correction_triage','agent','agent_recorded','t','t','subject',NULL,NULL,NULL,NULL,'{}',NULL)")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("triage uniqueness index was not installed")
