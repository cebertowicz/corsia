"""Pausing an agent pauses its workers all the way down (pause contract rule 1, #1970).

Each plan in the tree moves as its own recorded transition and names the pause
that carried it, so resuming the top brings back exactly what the cascade
paused. A plan the user paused on its own is where a cascade stops, both ways.
A running Turn is not killed: it is told at its next safe boundary to stop at a
safe point, and that telling can never start a Turn.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from automation import plan_liveness
from infra.passive_changes import append_change
from infra.schema import SCHEMA_SQL
from infra.thread_inputs import claim_safe_boundary, start_idle_turn
import plans.admission_projection as admission_projection
import plans.boundary as boundary
import plans.lifecycle as lifecycle
import plans.service as service
from plans.boundary import PlanActor

TURN = "turn-running"
USER = PlanActor(None, "user", None, kind="user")


def _db() -> sqlite3.Connection:
    """A top agent, the worker it started, and that worker's own worker."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    for thread_id, spawner in (("top", None), ("mid", "top"), ("leaf", "mid")):
        db.execute(
            """INSERT INTO threads
               (id,user_id,title,model,spawned_by,lifecycle_state,created_at,updated_at)
               VALUES (?,'user',?,'test/model',?,'active','t','t')""",
            (thread_id, thread_id.title(), spawner),
        )
    db.commit()
    for thread_id in ("top", "mid", "leaf"):
        service.ensure_plan("thread", thread_id, db=db)
    db.commit()
    return db


def _plan(db, thread_id: str):
    return db.execute(
        "SELECT * FROM plans WHERE owner_kind='thread' AND owner_id=?", (thread_id,),
    ).fetchone()


def _states(db) -> dict[str, str]:
    return {thread: _plan(db, thread)["state"] for thread in ("top", "mid", "leaf")}


def _change(db, thread_id: str, operation: str) -> dict:
    """Pause or resume one agent's plan as the user."""
    plan = _plan(db, thread_id)
    return boundary.mutate(
        plan["id"], operation, {"expected_revision": plan["revision"]},
        actor=USER, provenance="test.pause_cascade",
        mutation_id=f"{operation}-{thread_id}-{plan['revision']}", db=db,
    ).result


def _running_turn(db, thread_id: str) -> None:
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,?,0,'test/model','running','t','boot')""",
        (TURN, thread_id),
    )
    db.commit()


def _pause_inputs(db, thread_id: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM messages WHERE thread_id=? AND id LIKE 'plan-wake:paused:%'",
        (thread_id,),
    ).fetchall()


def test_pause_carries_every_worker_below_and_resume_brings_them_back():
    db = _db()

    result = _change(db, "top", "pause")

    assert _states(db) == {"top": "paused", "mid": "paused", "leaf": "paused"}
    assert result["cascaded_plan_ids"] == [_plan(db, "mid")["id"], _plan(db, "leaf")["id"]]
    top_id = _plan(db, "top")["id"]
    for thread_id in ("mid", "leaf"):
        event = db.execute(
            """SELECT provenance, payload_json FROM plan_events
               WHERE plan_id=? AND event='state_transition'""",
            (_plan(db, thread_id)["id"],),
        ).fetchone()
        assert event["provenance"] == lifecycle.CASCADE_PROVENANCE
        # Neither worker was running or had a wake armed (#2325).
        assert json.loads(event["payload_json"]) == {"cascade_root": top_id, "quiet": True}

    result = _change(db, "top", "resume")

    assert _states(db) == {"top": "active", "mid": "active", "leaf": "active"}
    assert len(result["cascaded_plan_ids"]) == 2


def test_a_worker_paused_on_its_own_is_where_a_cascade_stops():
    db = _db()
    _change(db, "mid", "pause")

    assert _change(db, "top", "pause")["cascaded_plan_ids"] == []
    assert _change(db, "top", "resume")["cascaded_plan_ids"] == []
    assert _states(db) == {"top": "active", "mid": "paused", "leaf": "paused"}

    _change(db, "mid", "resume")
    assert _states(db) == {"top": "active", "mid": "active", "leaf": "active"}


def test_resume_passes_through_a_running_worker_to_the_workers_a_cascade_paused():
    db = _db()
    _change(db, "top", "pause")
    # A worker running again while its own worker stays cascade-paused.
    db.execute("UPDATE plans SET state='active' WHERE id=?", (_plan(db, "mid")["id"],))
    db.commit()

    _change(db, "top", "resume")

    assert _states(db) == {"top": "active", "mid": "active", "leaf": "active"}


def test_a_running_turn_is_told_to_stop_and_an_idle_worker_is_not_started():
    db = _db()
    _running_turn(db, "leaf")

    _change(db, "top", "pause")

    assert _pause_inputs(db, "mid") == []
    told = _pause_inputs(db, "leaf")
    assert len(told) == 1
    assert told[0]["delivery_intent"] == "apply_now"
    assert "together with the agent that started you" in told[0]["content"]
    assert "Stop at the next safe point" in told[0]["content"]
    batch = claim_safe_boundary("leaf", TURN, db=db)
    assert batch is not None
    # The digest of what else changed rides ahead of it, as with every wake.
    assert batch.items[-1].message_id == told[0]["id"]


def test_a_pause_whose_turn_ended_first_never_starts_one():
    db = _db()
    _running_turn(db, "top")
    _change(db, "top", "pause")
    told = _pause_inputs(db, "top")
    assert "together with" not in told[0]["content"]
    db.execute("UPDATE active_turns SET status='completed' WHERE id=?", (TURN,))
    db.commit()

    started = start_idle_turn(
        "top", turn_id="next", boot_epoch="boot", started_at="t", db=db,
    )

    assert started is None
    row = db.execute(
        "SELECT delivery_state FROM messages WHERE id=?", (told[0]["id"],),
    ).fetchone()
    assert row["delivery_state"] == "cancelled"


def test_a_delegated_step_reads_paused_while_its_worker_is_paused():
    db = _db()
    node = service.add_node(
        _plan(db, "top")["id"], kind="task", title="Delegated work",
        acceptance_criteria="The worker returns it", db=db,
    )
    db.execute(
        "UPDATE plan_nodes SET state='active', spawned_thread_id='mid' WHERE id=?",
        (node.id,),
    )
    db.commit()

    _change(db, "mid", "pause")
    projected = admission_projection.project(node.id, db=db)

    assert projected["condition"] == "paused"
    assert projected["primary_reason"] == "Its worker is paused"


# Resume restores what the pause stopped, not more (#2325).

MID = PlanActor("mid", "user", None)


def _own_task(db, thread_id: str = "mid") -> None:
    """The worker adds and starts its own task, as a worker does; nobody is told."""
    plan = _plan(db, thread_id)
    added = boundary.mutate(
        plan["id"], "add",
        {"kind": "task", "title": "Worker task", "acceptance_criteria": "Done"},
        actor=MID, provenance="test.pause_cascade", mutation_id=f"add-{thread_id}", db=db,
    ).result
    node = added.get("node", added)
    boundary.mutate(
        plan["id"], "start",
        {"node_id": node["id"], "expected_revision": node["revision"]},
        actor=MID, provenance="test.pause_cascade", mutation_id=f"start-{thread_id}", db=db,
    )


def _selection_wakes(db, thread_id: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'plan-wake:work_selected:%'",
        (thread_id,),
    ).fetchone()[0]


def _liveness(db, thread_id: str) -> str | None:
    row = db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id=?", (thread_id,),
    ).fetchone()
    return None if row is None else row["status"]


def _finished_worker(db, ended: str = plan_liveness.STALLED) -> None:
    """A worker with open work whose liveness episode ended long ago.

    ``stalled`` is where an exhausted budget leaves it; ``cancelled`` is where
    Stop, or a check that found nothing actionable, leaves it.
    """
    _own_task(db)
    wake = plan_liveness.on_turn_complete("mid", db=db)
    assert wake is not None
    assert plan_liveness._claim_fire(wake.id, ended, db)
    db.commit()


@pytest.mark.parametrize("ended", [plan_liveness.STALLED, plan_liveness.CANCELLED])
def test_resuming_an_agent_does_not_start_a_worker_that_was_not_working(ended):
    db = _db()
    _finished_worker(db, ended)
    before = _selection_wakes(db, "mid")

    _change(db, "top", "pause")
    _change(db, "top", "resume")

    assert _states(db)["mid"] == "active"
    assert _selection_wakes(db, "mid") == before
    assert _liveness(db, "mid") != "armed"


def test_resuming_an_agent_restarts_a_worker_whose_wake_was_armed():
    db = _db()
    _own_task(db)
    plan_liveness.on_turn_complete("mid", db=db)
    db.commit()
    assert _liveness(db, "mid") == "armed"

    _change(db, "top", "pause")
    _change(db, "top", "resume")

    assert _selection_wakes(db, "mid") == 1
    assert _liveness(db, "mid") == "armed"


def test_a_quiet_worker_that_was_sent_something_while_paused_is_restarted():
    db = _db()
    _finished_worker(db)
    _change(db, "top", "pause")
    append_change(
        db, object_kind="plan", object_id=str(_plan(db, "mid")["id"]),
        change_kind="held", summary="Arrived while paused: a review verdict",
    )
    db.commit()

    _change(db, "top", "resume")

    assert _selection_wakes(db, "mid") == 1
