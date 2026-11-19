"""plan_activity counts decisions raised to the person, by the raising plan's origin."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from infra.audit_plan_activity import plan_activity
from infra.schema import SCHEMA_SQL
from plans import decisions, service
from tests.test_attention_queue import _seed, raise_decision
from tests.test_plan_conductor_task_adds import db  # noqa: F401


def _raise(db, thread_id: str, *, times: int = 1) -> None:
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(plan.id, kind="task", title="Decide", acceptance_criteria="Answered", db=db)
    for _ in range(times):
        service._event(db, node.id, "decision_raised", actor_id=thread_id, provenance="test.decision")
    db.commit()


def _server_raise(db, thread_id: str) -> None:
    """Raise through CORSIA's own raise path, under a provenance no named writer uses."""
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(plan.id, kind="task", title="Offer", acceptance_criteria="Answered", db=db)
    decisions.raise_offer(
        db, plan_id=plan.id, source_node_id=node.id, provenance="test.some_server_writer",
        content={"title": "Take this over?", "context": "c", "tradeoff": "t", "recommendation": "A",
                 "rationale": "r", "options": [{"id": "A", "label": "Yes"}, {"id": "B", "label": "No"}],
                 "other_enabled": True},
    )
    db.commit()


def test_decisions_raised_split_by_root_or_delegated_and_who_raised(db):
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,spawned_by,title,model,created_at,updated_at)"
        " VALUES ('worker','user','project-a','lane','worker','test/model','t','t')"
    )
    _raise(db, "lane", times=2)
    _server_raise(db, "lane")
    _raise(db, "worker")
    service.add_node(service.ensure_plan("thread", "worker", db=db).id, kind="task", title="Plain",
                     acceptance_criteria="Done", db=db)
    db.commit()
    now = datetime.now(timezone.utc)

    row = plan_activity(db, "user", (now - timedelta(hours=1)).isoformat(), (now + timedelta(hours=1)).isoformat())[0]

    assert {(r["origin"], r["raised_by"], r["archived"], r["decisions"], r["raises"])
            for r in row["decisions_raised"]} == {
        ("root", "agent", 0, 1, 2),
        ("root", "server", 0, 1, 1),
        ("delegated", "agent", 0, 1, 1),
    }
    assert "decisions_raised" in row["definitions"]


def test_decisions_raised_outside_the_window_are_not_counted(db):
    _raise(db, "lane")
    later = datetime.now(timezone.utc) + timedelta(days=2)

    row = plan_activity(db, "user", later.isoformat(), (later + timedelta(hours=1)).isoformat())[0]

    assert row["decisions_raised"] == []


@pytest.fixture
def attention_db():
    """The attention-queue threads: root work and a delegated child under it."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    _seed(connection)
    connection.commit()
    yield connection
    connection.close()


def test_a_decision_raised_through_the_raise_path_counts_as_an_agent_raise(attention_db):
    raise_decision(attention_db, "child", "child")
    now = datetime.now(timezone.utc)

    row = plan_activity(attention_db, "user-1", (now - timedelta(hours=1)).isoformat(),
                        (now + timedelta(hours=1)).isoformat())[0]

    assert [(r["origin"], r["raised_by"], r["decisions"]) for r in row["decisions_raised"]] == [
        ("delegated", "agent", 1),
    ]
