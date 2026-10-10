"""Actor-bound current-plan read/ensure and structured context delivery."""

from __future__ import annotations

from collections.abc import Iterator

import json
import sqlite3

import pytest

from core.dynamic_context_registry import collect_context_sources
from infra.schema import SCHEMA_SQL
from plans import service
from plans.boundary import PlanActor


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    yield connection
    connection.close()


def _session(
    db, thread_id: str, *, user_id: str = "user-1", project_id: str | None = None
):
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,created_at,updated_at)
           VALUES (?,?,?,?,?)""",
        (thread_id, user_id, project_id, "t", "t"),
    )
    db.commit()


class _DbProxy:
    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)

    def close(self):
        pass


def test_thread_actor_read_and_ensure_are_current_owner_bound_and_idempotent(db):
    from plans.boundary import ensure_current_plan, read_current_plan

    _session(db, "owner")
    actor = PlanActor("owner", "user-1", None)

    assert read_current_plan(actor=actor, db=db) is None
    first = ensure_current_plan(actor=actor, db=db)
    second = ensure_current_plan(actor=actor, db=db)

    assert first == second
    assert first["plan"]["owner_id"] == "owner"
    assert first["nodes"] == []


def test_only_a_thread_actor_reads_its_own_current_plan(db):
    from plans.boundary import ensure_current_plan, read_current_plan

    _session(db, "owner", user_id="user-1")
    human = PlanActor(None, "user-1", None, kind="user")

    with pytest.raises(PermissionError):
        read_current_plan(actor=human, db=db)
    with pytest.raises(PermissionError):
        ensure_current_plan(actor=human, db=db)


def test_current_plan_projection_contains_ordered_tree_and_derived_blocked(db):
    from plans.boundary import read_current_plan

    _session(db, "owner")
    actor = PlanActor("owner", "user-1", None)
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(
        plan.id,
        kind="phase",
        title="Phase",
        acceptance_criteria="Phase DOD",
        db=db,
    )
    prerequisite = service.add_node(
        plan.id,
        kind="task",
        title="First",
        acceptance_criteria="First DOD",
        parent_id=phase.id,
        db=db,
    )
    dependent = service.add_node(
        plan.id,
        kind="task",
        title="Second",
        acceptance_criteria="Second DOD",
        parent_id=phase.id,
        db=db,
    )
    service.add_dependency(dependent.id, prerequisite.id, db=db)

    current = read_current_plan(actor=actor, db=db)

    assert [node["title"] for node in current["nodes"]] == [
        "Phase",
        "First",
        "Second",
    ]
    assert list(current["nodes"][2]["unmet_dependencies"]) == [prerequisite.id]
    assert current["progress"]["total"] == 2


@pytest.mark.asyncio
async def test_tools_bind_read_and_ensure_to_current_execution_actor(
    db, monkeypatch
):
    from tools import plans as tool_plans

    _session(db, "owner")
    proxy = _DbProxy(db)

    async def immediate(callable_):
        return callable_()

    monkeypatch.setattr(
        tool_plans,
        "_require_context",
        lambda _kwargs: (
            "owner",
            {"user_id": "user-1", "project_id": None},
        ),
    )
    monkeypatch.setattr(tool_plans, "get_db", lambda: proxy)
    monkeypatch.setattr(tool_plans, "run_in_db", immediate)

    assert json.loads(await tool_plans.PlanReadTool().execute()) is None
    first = json.loads(await tool_plans.PlanEnsureTool().execute())
    second = json.loads(await tool_plans.PlanEnsureTool().execute())

    assert first["plan"]["id"] == second["plan"]["id"]
    assert tool_plans.PlanReadTool().parameters["additionalProperties"] is False
    assert tool_plans.PlanEnsureTool().parameters["required"] == []


def test_current_plan_tools_are_cataloged_for_external_harnesses():
    from tools.catalog import TOOL_CATALOG

    assert {
        name: (TOOL_CATALOG[name].category, TOOL_CATALOG[name].authority)
        for name in ("plan_read", "plan_ensure", "plan_update")
    } == {
        "plan_read": ("plans", "plan-read"),
        "plan_ensure": ("plans", "plan-write"),
        "plan_update": ("plans", "plan-write"),
    }
    assert all(
        TOOL_CATALOG[name].external_harness
        for name in ("plan_read", "plan_ensure", "plan_update")
    )


def test_plan_and_artifact_state_stays_out_of_provider_context(db, tmp_path):
    _session(db, "owner")
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title="Implement current plan context",
        acceptance_criteria="Context test passes",
        description="Durable node intent is visible in context.",
        db=db,
    )
    service.transition_node(node.id, "active", db=db)

    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,title,state,created_at,updated_at)
           VALUES ('artifact','workspace','document','notes.md','Notes',
                   'active','t','t')"""
    )
    db.execute(
        """INSERT INTO artifact_registrations
           (id,artifact_id,parent_thread_id,relationship_state,created_at,updated_at)
           VALUES ('registration','artifact','owner','owner-active','t','t')"""
    )
    db.commit()

    collection = collect_context_sources(
        thread_id="owner",
        project_id=None,
        workspace=tmp_path,
        db=db,
    )
    source_ids = {source.source_id for source in collection.sources}
    assert "dynamic:structured-current-plan" not in source_ids
    assert "dynamic:artifacts-table" not in source_ids
    assert "dynamic:runtime-context" not in source_ids
