"""Schema 131 -> 132: durable task intent and supporting words."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_131_to_132_plan_intent import migrate_131_to_132
from infra.schema import SCHEMA_VERSION
from plans import service


def _dev_131() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    db.execute("ALTER TABLE plan_nodes DROP COLUMN intent_node_id")
    db.execute("ALTER TABLE plan_nodes DROP COLUMN intent")
    db.execute("DROP TABLE plan_node_intent_words")
    db.execute("PRAGMA user_version=131")
    db.commit()
    return db


def test_v131_registers_the_task_intent_step() -> None:
    assert SCHEMA_VERSION == 132
    assert MIGRATIONS[131] is migrate_131_to_132


def test_existing_plan_nodes_survive_the_intent_migration() -> None:
    with closing(_dev_131()) as db:
        plan = service.ensure_plan("thread", "root", db=db)
        node = service.add_node(
            plan.id,
            title="Existing task",
            kind="task",
            db=db,
        )
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.from_version, result.to_version) == (131, 132)
        row = db.execute("SELECT intent,intent_node_id FROM plan_nodes WHERE id=?", (node.id,)).fetchone()
        assert tuple(row) == (None, None)
