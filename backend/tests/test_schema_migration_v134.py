"""Schema 133 -> 134: durable task intent and supporting words."""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_133_to_134_plan_intent import migrate_133_to_134
from infra.schema import SCHEMA_VERSION
from plans import service


def _dev_133() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    db.execute("DROP INDEX idx_plan_nodes_intent_node")
    db.execute("ALTER TABLE plan_nodes DROP COLUMN intent_node_id")
    db.execute("ALTER TABLE plan_nodes DROP COLUMN intent")
    db.execute("DROP TABLE plan_node_intent_words")
    db.execute("PRAGMA user_version=133")
    db.commit()
    return db


def test_v133_registers_the_task_intent_step() -> None:
    assert SCHEMA_VERSION == 134
    assert MIGRATIONS[133] is migrate_133_to_134


def test_existing_plan_nodes_survive_the_intent_migration() -> None:
    with closing(_dev_133()) as db:
        plan = service.ensure_plan("thread", "root", db=db)
        node = service.add_node(
            plan.id,
            title="Existing task",
            kind="task",
            acceptance_criteria="Existing task remains unchanged",
            db=db,
        )
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.from_version, result.to_version) == (133, 134)
        row = db.execute("SELECT intent,intent_node_id FROM plan_nodes WHERE id=?", (node.id,)).fetchone()
        assert tuple(row) == (None, None)


def test_pre_v134_node_creation_refuses_intent_rather_than_dropping_it() -> None:
    """A migration running the plan service before v134 has no intent columns to store it in."""
    with closing(_dev_133()) as db:
        plan = service.ensure_plan("thread", "root", db=db)
        with pytest.raises(service.PlanInputError, match="task intent requires schema version 134"):
            service.add_node(
                plan.id, kind="task", title="Reject intent",
                acceptance_criteria="Must require schema 134", intent="Keep the request", db=db,
            )
