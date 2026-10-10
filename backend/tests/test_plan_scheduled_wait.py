"""An agent sets a task aside by scheduling it, never by pausing it (W1, W2).

- ``arm_watch`` with a ``reason`` and a deadline schedules the task: no
  artifact, no expected event, no upper bound on the date.
- Only the plan's own agent schedules; the reply says when the task comes back.
- The row leads with the date, then why it was set aside.
- When the date arrives the wake names the task, how long it was aside and
  why; every other watch keeps the one fixed summary.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import uuid

import pytest

from automation.continuation import runner
from automation.continuation.sources.watch_expiry import WatchExpirySource
from infra.database import get_db
from infra.schema import SCHEMA_SQL
from plans import admission, scheduled_wait, service
from plans.boundary import PlanActor, mutate
from tests.event_routing_helpers import mk_session

OWNER = PlanActor("owner", "user", "project")
USER = PlanActor(None, "user", "project", kind="user")
REASON = "Revisit after the design review"


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')"
    )
    connection.execute(
        """INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
           VALUES ('owner','user','project','test/model','t','t')"""
    )
    connection.commit()
    yield connection
    connection.close()


def _task(db, owner: str = "owner"):
    plan = service.ensure_plan("thread", owner, db=db)
    node = service.add_node(
        plan.id, kind="task", title="Tidy the index", acceptance_criteria="Tidied", db=db,
    )
    db.commit()
    return plan, node


def _due(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).replace(microsecond=0).isoformat()


def _schedule(db, plan, node, *, actor=OWNER, **params):
    fields = {
        "node_id": node.id, "reason": REASON,
        "condition": {"type": "deadline", "value": _due(days=30)},
        "expected_revision": service._node_row(db, node.id)["revision"], **params,
    }
    result = mutate(
        plan.id, "arm_watch", {k: v for k, v in fields.items() if v is not None},
        actor=actor, provenance="test.scheduled_wait", mutation_id=uuid.uuid4().hex, db=db,
    ).result
    db.commit()
    return result


def _armed(db, node_id: str) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT id, condition_type, condition_value, expected_event FROM expected_event_watches
           WHERE plan_node_id=? AND status='armed'""",
        (node_id,),
    ).fetchall()


def test_an_agent_schedules_a_task_months_out_with_a_reason(db):
    plan, node = _task(db)
    due = _due(days=90)

    result = _schedule(db, plan, node, condition={"type": "deadline", "value": due})

    [watch] = _armed(db, node.id)
    assert (watch["condition_type"], watch["expected_event"]) == ("deadline", "scheduled")
    assert datetime.fromisoformat(watch["condition_value"]) == datetime.fromisoformat(due)
    assert result["wait"] == (
        f"Scheduled until {due}. Watch {watch['id']} wakes you then. "
        "To pick it up sooner, call resolve_watch with this watch_id."
    )


def test_the_row_says_the_date_then_why(db):
    plan, node = _task(db)
    due = _due(days=2)

    _schedule(db, plan, node, condition={"type": "deadline", "value": due})

    projected = admission.project(node.id, db=db)
    until = datetime.fromisoformat(due).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    assert projected["condition"] == "waiting"
    [cause] = projected["causes"]
    assert cause["reason"] == f"Until {until} · {REASON}"
    assert cause["scheduled_reason"] == REASON


@pytest.mark.parametrize(("params", "refusal"), [
    ({"artifact_id": "artifact"},
     "To wait for an event on the artifact, drop reason; to schedule the task, drop artifact_id"),
    ({"condition": {"type": "event", "value": "ci.done"}}, "must be a deadline"),
    ({"reason": None}, "needs the expected_event that ends the wait, or a reason"),
])
def test_a_schedule_is_a_reason_and_a_date(db, params, refusal):
    plan, node = _task(db)

    with pytest.raises(service.PlanInputError, match=refusal):
        _schedule(db, plan, node, **params)

    assert _armed(db, node.id) == []


def test_the_reason_field_says_it_schedules_and_takes_no_artifact():
    # #3237: the description is where an agent arming an event watch learns
    # to leave reason out, before the refusal costs it a call.
    from tools.plan_field_schemas import MUTATION_FIELD_SCHEMAS

    description = MUTATION_FIELD_SCHEMAS["reason"]["description"]
    assert "schedules the task" in description
    assert "artifact_id" in description


def test_only_the_plans_own_agent_schedules(db):
    plan, node = _task(db)

    with pytest.raises(Exception, match="only that agent schedules one"):
        _schedule(db, plan, node, actor=USER)

    assert _armed(db, node.id) == []


def _deliveries():
    seen: list[tuple[str, str, str]] = []

    async def deliver(thread_id, event, summary, _details):
        seen.append((thread_id, event, summary))
        return True

    return seen, deliver


def _sweep_at(due: str) -> str:
    return (datetime.fromisoformat(due) + timedelta(minutes=1)).isoformat()


@pytest.mark.asyncio
async def test_a_due_scheduled_task_wakes_its_agent_with_what_and_why(db):
    plan, node = _task(db)
    due = _due(days=3, minutes=1)
    _schedule(db, plan, node, condition={"type": "deadline", "value": due})
    seen, deliver = _deliveries()

    await runner.sweep(
        _sweep_at(due), db=db, sources=[WatchExpirySource()],
        is_idle=lambda _thread: True, deliver=deliver,
    )

    assert seen == [("owner", "plan.watch_expired", (
        f'Scheduled task "Tidy the index" is due (task {node.id}). '
        f"You set it aside 3 days ago: {REASON}. "
        "Start it now, schedule it again with a reason, close it, or raise a decision."
    ))]


@pytest.mark.asyncio
async def test_an_event_watch_keeps_the_fixed_wake(db):
    plan, node = _task(db)
    due = _due(minutes=5)
    _schedule(
        db, plan, node, reason=None, expected_event="external.done",
        condition={"type": "deadline", "value": due},
    )
    seen, deliver = _deliveries()

    await runner.sweep(
        _sweep_at(due), db=db, sources=[WatchExpirySource()],
        is_idle=lambda _thread: True, deliver=deliver,
    )

    [(_thread, _event, summary)] = seen
    assert "Scheduled task" not in summary
    assert scheduled_wait.wake_summary(db, type("Watch", (), {
        "id": "none", "plan_node_id": node.id, "condition": {"type": "deadline"},
    })) is None


@pytest.mark.asyncio
async def test_the_scheduled_wake_reads_its_own_connection(isolated_db):
    thread_id = mk_session("scheduled-own-connection")
    setup = get_db()
    plan, node = _task(setup, owner=thread_id)
    due = _due(hours=5, minutes=1)
    _schedule(
        setup, plan, node, actor=PlanActor(thread_id, "user-1", None),
        condition={"type": "deadline", "value": due},
    )
    seen, deliver = _deliveries()

    await runner.sweep(
        _sweep_at(due), db=None, sources=[WatchExpirySource()],
        is_idle=lambda _thread: True, deliver=deliver,
    )

    [(woken, _event, summary)] = seen
    assert woken == thread_id
    assert summary.startswith('Scheduled task "Tidy the index" is due')
    assert f"You set it aside 5 hours ago: {REASON}." in summary
