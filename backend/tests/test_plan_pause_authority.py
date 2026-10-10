"""Pause is the user's (pause contract rule 1, #1969; W1 of the work-never-stops design).

Only the user pauses or resumes. An agent's pause is refused in one line that
says how to schedule the task instead, even in a Turn carrying the user's word:
the agent relays the user by scheduling, and the user pauses from the pane.
"""

from __future__ import annotations

import sqlite3

import pytest

from infra import queries
from infra.schema import SCHEMA_SQL
import plans.boundary as boundary
import plans.service as service
from plans.boundary import PlanActor

HUMAN = PlanActor(None, "user", None, kind="user")


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('owner','user','test/model','t','t')"
    )
    db.commit()
    return db


def _agent(turn_id: str | None = "turn-1") -> PlanActor:
    return PlanActor("owner", "user", None, turn_id=turn_id)


def _deliver(db, message_id: str, role: str, metadata: dict, *, turn_id: str = "turn-1") -> None:
    """A delivered input of ``turn_id``; the real claim path is proven separately."""
    queries.insert_message("owner", role, "text", metadata, message_id, db=db)
    db.execute("UPDATE messages SET active_turn_id=? WHERE id=?", (turn_id, message_id))
    db.commit()


def _change(db, operation: str, actor: PlanActor, key: str):
    plan = service.ensure_plan("thread", "owner", db=db)
    return boundary.mutate(
        plan.id, operation,
        {"expected_revision": service._plan_row(db, plan.id)["revision"]},
        actor=actor, provenance="test.pause_authority", mutation_id=key, db=db,
    ).result["plan"]


SCHEDULE_INSTEAD = (
    "Pause is the user's. To come back to this later, schedule it: "
    "arm_watch(node_id={node}, condition={{type: deadline, value: <ISO time>}}, reason=<why>)."
)


@pytest.mark.parametrize(
    ("role", "metadata"),
    [
        ("user", {}),
        ("system", {"type": "context_event", "event_type": "plan.decision_answered"}),
        ("user", {"sender_kind": "agent"}),
    ],
    ids=["users-message", "decision-answer", "peer-message"],
)
def test_an_agent_pause_of_its_plan_is_refused_even_with_the_users_word(role, metadata):
    db = _db()
    _deliver(db, "m1", role, metadata)
    with pytest.raises(service.PlanRefusal) as refused:
        _change(db, "pause", _agent(), "relay")
    assert str(refused.value) == SCHEDULE_INSTEAD.format(node="<task id>")
    assert service.ensure_plan("thread", "owner", db=db).state == "active"


def test_an_agent_pause_of_a_task_names_that_task_in_the_schedule_line():
    db = _db()
    _deliver(db, "m1", "user", {})
    plan = service.ensure_plan("thread", "owner", db=db)
    task = service.add_node(plan.id, kind="task", title="T", acceptance_criteria="Done", db=db)
    db.commit()

    with pytest.raises(service.PlanRefusal) as refused:
        boundary.mutate(
            plan.id, "pause_node",
            {"node_id": task.id, "reason": "Later", "expected_revision": task.revision},
            actor=_agent(), provenance="test.pause_authority", mutation_id="node", db=db,
        )

    assert str(refused.value) == SCHEDULE_INSTEAD.format(node=task.id)
    assert db.execute("SELECT COUNT(*) FROM plan_node_holds").fetchone()[0] == 0


def test_the_user_still_pauses_a_task():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    task = service.add_node(plan.id, kind="task", title="T", acceptance_criteria="Done", db=db)
    db.commit()

    boundary.mutate(
        plan.id, "pause_node",
        {"node_id": task.id, "reason": "Later", "expected_revision": task.revision},
        actor=HUMAN, provenance="test.pause_authority", mutation_id="node", db=db,
    )

    assert db.execute(
        "SELECT owner_kind FROM plan_node_holds WHERE cleared_at IS NULL"
    ).fetchone()[0] == "user"


def test_only_the_user_resumes():
    db = _db()
    _change(db, "pause", HUMAN, "user-pause")
    _deliver(db, "m1", "user", {})

    with pytest.raises(service.PlanRefusal, match="only the user resumes"):
        _change(db, "resume", _agent(), "agent-resume")

    assert _change(db, "resume", HUMAN, "user-resume")["state"] == "active"

