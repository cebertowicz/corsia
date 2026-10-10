"""Schema 91 adds nullable author-declared plan-node size."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from infra.database import initialize_schema
from infra.migration_90_to_91_plan_node_size import migrate_90_to_91
from plans import service


def test_v91_adds_nullable_size_with_the_canonical_check():
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE plan_nodes (id TEXT PRIMARY KEY)")
        migrate_90_to_91(db)
        migrate_90_to_91(db)

        db.execute("INSERT INTO plan_nodes (id,size) VALUES ('sized','XL')")
        db.execute("INSERT INTO plan_nodes (id,size) VALUES ('unsized',NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO plan_nodes (id,size) VALUES ('invalid','huge')")


def test_pre_v51_node_creation_refuses_size_and_workflow_fields():
    """A migration running the plan service before v51 has no workflow or size columns."""
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        initialize_schema(db)
        for column in ("size", "workflow_ref", "workflow_version", "workflow_step", "difficulty"):
            db.execute(f"ALTER TABLE plan_nodes DROP COLUMN {column}")
        db.commit()

        plan = service.ensure_plan("thread", "migration", db=db)
        with pytest.raises(service.PlanInputError, match="size requires schema version 91"):
            service.add_node(
                plan.id, kind="task", title="Reject size",
                acceptance_criteria="Must require schema 91", size="M", db=db,
            )
        with pytest.raises(service.PlanInputError, match="workflow fields require schema version 51"):
            service.add_node(
                plan.id, kind="task", title="Reject workflow",
                acceptance_criteria="Must require schema 51", workflow_ref="x", db=db,
            )
