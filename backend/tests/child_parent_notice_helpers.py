"""Shared fixtures for the child-to-parent notification matrix tests.

One delegation: a parent thread holds an active delegated step, the worker
owns its own plan below it. Every row in the matrix is a write in the worker's
plan, read back as what the parent was told -- a passive digest row on the
parent's plan, a wake in the parent's own input FIFO, or nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from plans import mutations, service
from plans.boundary import PlanActor

#: The digest row every plan write leaves for its own owner; never a notice.
STATUS_KIND = "plan.status"

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)
DEADLINE = (NOW + timedelta(hours=6)).isoformat()


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Yield one seeded lineage, imported by each matrix module that needs it."""
    connection = seeded_db()
    yield connection
    connection.close()


def begin_worker_turn(
    db, turn_id: str = "turn-1", *, started_at: str = "2000-01-01T00:00:00",
) -> None:
    """Record the worker's running Turn as the Turn runtime writes it: naive UTC."""
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,'worker',0,'native/model','running',?,'boot-1')""",
        (turn_id, started_at),
    )


def end_worker_turn(db) -> None:
    """Report the worker's Turn ending, as the harness does."""
    from automation import plan_liveness_terminal
    from automation.turn_end_effects import PendingTurnEndEffects

    plan_liveness_terminal.on_thread_turn_complete(
        "worker", db=db, effects=PendingTurnEndEffects(),
    )


def worker_actor() -> PlanActor:
    """Return the worker acting on its own plan."""
    return PlanActor("worker", "user-1", "project-1")


def seeded_db() -> sqlite3.Connection:
    """Return an in-memory database with one grandparent/parent/worker lineage."""
    from infra.schema import SCHEMA_SQL

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    for thread_id, spawned_by in (
        ("grandparent", None),
        ("parent", "grandparent"),
        ("worker", "parent"),
    ):
        connection.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1','project-1',?,'native/model','t','t')""",
            (thread_id, spawned_by),
        )
    connection.commit()
    return connection


def delegated(db, *, title="Work", with_worker_node=True):
    """Return (parent delegated node, worker plan, worker's first step or None)."""
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    delegated_step = service.add_node(
        parent_plan.id, kind="task", title="Delegate work",
        acceptance_criteria="Worker result is reviewed",
        spawn_kind="claude-code", db=db,
    )
    service.transition_node(delegated_step.id, "active", db=db)
    plan = service.ensure_plan("thread", "worker", db=db)
    node = None
    if with_worker_node:
        node = service.add_node(
            plan.id, kind="task", title=title,
            acceptance_criteria="Tests pass", db=db,
        )
    mutations.attach_spawned_thread(delegated_step.id, "worker", db=db)
    return delegated_step, plan, node


def other_open_step(db, plan, *, title="Still running"):
    """Add and start a second worker step, so the worker has not returned."""
    node = service.add_node(
        plan.id, kind="task", title=title, acceptance_criteria="Done", db=db,
    )
    service.transition_node(node.id, "active", db=db)
    return node


def notices(db, thread_id="parent") -> list[sqlite3.Row]:
    """Return the digest rows this thread's plan was given, oldest first."""
    return db.execute(
        """SELECT journal.* FROM context_change_journal AS journal
           JOIN plans ON plans.id=journal.object_id
           WHERE journal.object_kind='plan' AND journal.change_kind<>?
             AND plans.owner_kind='thread' AND plans.owner_id=?
           ORDER BY journal.seq""",
        (STATUS_KIND, thread_id),
    ).fetchall()


def notice_kinds(db, thread_id="parent") -> list[str]:
    """Return just the change kinds this thread was told about."""
    return [str(row["change_kind"]) for row in notices(db, thread_id)]


def notice_details(row: sqlite3.Row) -> dict:
    """Return one digest row's decoded details."""
    return json.loads(row["details_json"] or "{}")


def wakes(db, thread_id="parent") -> list[sqlite3.Row]:
    """Return the plan wakes appended to this thread's own input FIFO."""
    return db.execute(
        """SELECT * FROM messages
           WHERE thread_id=? AND id LIKE 'plan-wake:%' ORDER BY input_order""",
        (thread_id,),
    ).fetchall()


def transcript(db, thread_id="parent") -> list[sqlite3.Row]:
    """Return the activity receipts written into this thread's transcript."""
    return db.execute(
        "SELECT * FROM messages WHERE thread_id=? AND id LIKE 'plan-event:%'"
        " ORDER BY seq",
        (thread_id,),
    ).fetchall()
