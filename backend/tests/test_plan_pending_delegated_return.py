"""A worker attached to a step its parent never started still returns (#2199).

Fan-out is the normal shape: the parent's one work slot is held by another
task while several workers run, so each delegated step stays ``pending``. The
worker owns that step's execution, not the parent's slot. When it returns, the
parent gets the one result-bearing wake and the step moves to review, and the
task the parent is working keeps its slot.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from automation.continuation import runner
from automation.continuation.sources.harvest import HarvestSource
from infra.schema import SCHEMA_SQL
from plans import admission_attempts, closure, mutations, service
from tests.plan_review_helpers import submit_review_evidence

NOW = "2026-08-01T00:00:00+00:00"


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    for thread_id, spawned_by in (("parent", None), ("worker", "parent")):
        db.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1','project-1',?,'test/model','t','t')""",
            (thread_id, spawned_by),
        )
    db.commit()
    return db


def _fan_out(db):
    """The parent works task A; step B is delegated but never started."""
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    held = service.add_node(
        parent_plan.id, kind="task", title="Task A",
        acceptance_criteria="Done", db=db,
    )
    service.transition_node(held.id, "active", actor_id="parent", db=db)
    delegated = service.add_node(
        parent_plan.id, kind="task", title="Delegated B",
        acceptance_criteria="Worker result is reviewed",
        spawn_kind="claude-code", db=db,
    )
    worker_plan = service.ensure_plan("thread", "worker", db=db)
    mutations.attach_spawned_thread(delegated.id, "worker", db=db)
    leaf = service.add_node(
        worker_plan.id, kind="task", title="Write the tests",
        acceptance_criteria="Done", db=db,
    )
    service.transition_node(leaf.id, "active", actor_id="worker", db=db)
    db.commit()
    return held, delegated, worker_plan, leaf


async def _sweep(db):
    await runner.sweep(
        NOW, db=db, sources=[HarvestSource()],
        is_idle=lambda _thread: True, deliver=AsyncMock(return_value=True),
    )
    await asyncio.sleep(0)


def _wakes(db) -> list[sqlite3.Row]:
    """Return wakes only: starting task A itself wakes the parent with its selection."""
    return db.execute(
        """SELECT id,content FROM messages
           WHERE thread_id='parent' AND id LIKE 'plan-wake:child_returned:%'
           ORDER BY seq,rowid""",
    ).fetchall()


@pytest.mark.asyncio
async def test_a_never_started_delegated_step_returns_with_one_wake():
    db = _db()
    try:
        held, delegated, worker_plan, leaf = _fan_out(db)
        assert service.get_node(delegated.id, db=db).state == "pending"
        submit_review_evidence(
            leaf.id, db=db, evidence="Tests pass on three runs", actor_id="worker",
        )
        db.commit()

        await _sweep(db)
        await _sweep(db)

        node = service.get_node(delegated.id, db=db)
        assert node.state == "review_requested"
        wakes = _wakes(db)
        assert [row["id"] for row in wakes] == [
            f"plan-wake:child_returned:{delegated.id}:{node.revision}",
        ]
        returned = json.loads(wakes[0]["content"].split("\n", 1)[1])["returned"]
        assert returned["child_plan_id"] == worker_plan.id
        assert [step["evidence"] for step in returned["steps"]] == [
            "Tests pass on three runs",
        ]
        # The worker's return never took or released the parent's slot.
        assert service.get_node(held.id, db=db).state == "active"
        assert admission_attempts.open_attempt(db, held.id) is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_step_with_only_closed_work_returns_the_same_way():
    db = _db()
    try:
        _held, delegated, _plan, leaf = _fan_out(db)
        closure.close_node(
            leaf.id, reason="Not needed after all", actor_id="worker", db=db,
        )
        db.commit()

        await _sweep(db)

        assert service.get_node(delegated.id, db=db).state == "review_requested"
        assert len(_wakes(db)) == 1
    finally:
        db.close()
