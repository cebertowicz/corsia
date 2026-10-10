"""A task assigned to the user is never the agent's next step (#2181)."""

from __future__ import annotations

import sqlite3

from infra.schema import SCHEMA_SQL
from plans import operator_projection, service, status
from plans.boundary import PlanActor


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project','Project','t','t')"
    )
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,title,created_at,updated_at)
           VALUES ('owner','user','project','Plan owner','t','t')"""
    )
    db.commit()
    return db


def _task(plan_id: str, title: str, *, db, assignee: str | None = None):
    return service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} is proven", assignee=assignee, db=db,
    )


def _operator(db) -> dict:
    payload = operator_projection.read_thread(
        "owner", actor=PlanActor(None, "user", None, kind="user"), db=db,
    )
    return payload["plans"][0]


def _next_step(db, plan_id: str):
    plan_row = db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    actor = PlanActor("owner", "thread", "owner", kind="thread")
    return status.read(db, plan_row, actor=actor)["next_step"]


def test_the_agents_next_step_skips_a_task_the_user_owns():
    """Admission, liveness and ordering already refuse the user's task to the
    agent; the next step must agree, or every read points the agent at a step
    it may not take while its own ready work goes unnamed."""
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    theirs = _task(plan.id, "Sign the contract", db=db, assignee="user")
    mine = _task(plan.id, "Draft the summary", db=db)

    projected = _operator(db)
    assert projected["next_node_ids"] == [mine.id]
    labels = {node["id"]: node["display"]["label_key"] for node in projected["nodes"]}
    assert labels[theirs.id] != "next"
    assert _next_step(db, plan.id)["id"] == mine.id


def test_a_plan_holding_only_the_users_task_has_no_next_step_for_the_agent():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    _task(plan.id, "Sign the contract", db=db, assignee="user")

    assert _operator(db)["next_node_ids"] == []
    assert _next_step(db, plan.id) is None
