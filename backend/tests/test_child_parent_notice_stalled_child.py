"""#3317: a worker that cannot run reads stalled, never working.

Matrix rows F1 and G16. A worker is stalled when no Turn of its runs and
either its FIFO head is held for an unavailable model, or its last Turn
errored with nothing runnable queued. Its parent's step then reads waiting
with the cause, its own plan does not read advancing, and the parent hears
one F1 wake that names the held model and the recovery.
"""

from __future__ import annotations

import pytest

from infra.input_admission import accept_thread_input
from infra.send_target import SELECTED_MODEL_UNAVAILABLE
from plans import admission, delegation_child_lifecycle, projections, service, status
from plans.boundary import PlanActor
from tests.child_parent_notice_helpers import begin_worker_turn, db, delegated, wakes

__all__ = ["db"]

PARENT = PlanActor("parent", "user-1", "project-1")
WORKER = PlanActor("worker", "user-1", "project-1")


def _row(db, node_id: str, actor: PlanActor = PARENT) -> dict:
    plan = service.ensure_plan("thread", actor.thread_id, db=db)
    read = status.read(db, service._plan_row(db, plan.id), actor=actor)
    return next(row for row in read["current"]["rows"] if row["id"] == node_id)


def _queue(db, message_id: str = "m1") -> None:
    accept_thread_input(
        "worker", role="user", content="Continue", intent="apply_now",
        message_id=message_id, db=db,
    )


def _hold_head(db, cause: str = SELECTED_MODEL_UNAVAILABLE) -> None:
    """Leave the worker's head as admission holds it, with ``cause`` as its durable error."""
    _queue(db)
    db.execute(
        """UPDATE messages SET write_outcome='proved_no_write', delivery_error=?,
               target_model='native/model', active_turn_id=NULL
           WHERE id='m1'""",
        (cause,),
    )


def _turn(db, status_: str, turn_id: str = "turn-1") -> None:
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,'worker',0,'native/model',?,'2000-01-01T00:00:00','boot-1')""",
        (turn_id, status_),
    )


def _worker_liveness(db, plan) -> str:
    return projections.plan_state(plan.id, db=db, memo={}).progress.liveness


def _start(db, node) -> None:
    """Select the worker's step as admission does: its open attempt reads running."""
    service.transition_node(node.id, "active", db=db)
    attempt, refusal = admission.select(
        node.id, actor_id="worker", provenance="test", db=db,
    )
    assert attempt is not None, refusal


def test_a_held_worker_reads_stalled_not_working(db):
    step, plan, node = delegated(db)
    _start(db, node)
    assert _worker_liveness(db, plan) == "advancing"
    _turn(db, "error")
    _hold_head(db)

    row = _row(db, step.id)

    assert row["label"] == "waiting"
    assert row["reason"] == (
        "The delegated child is stalled: its model is unavailable"
    )
    assert _worker_liveness(db, plan) != "advancing"


def test_the_held_workers_own_step_reads_waiting_not_working(db):
    """Its own pane tells the same truth: selected, but nothing runs it."""
    _step, _plan, node = delegated(db)
    _start(db, node)
    assert _row(db, node.id, WORKER)["label"] == "working"
    _turn(db, "error")
    _hold_head(db)

    row = _row(db, node.id, WORKER)

    assert row["label"] == "waiting"
    assert row["reason"] == "Stalled: its model is unavailable"


def test_an_errored_worker_with_nothing_queued_reads_stalled(db):
    step, plan, node = delegated(db)
    _start(db, node)
    _turn(db, "error")

    row = _row(db, step.id)

    assert row["label"] == "waiting"
    assert row["reason"] == "The delegated child is stalled: its last Turn failed"
    assert _worker_liveness(db, plan) != "advancing"


def test_an_errored_worker_with_a_new_input_queued_is_working_again(db):
    """A runnable input after the error is the recovery the parent sent."""
    step, _plan, node = delegated(db)
    _start(db, node)
    _turn(db, "error")
    _queue(db)

    assert _row(db, step.id)["reason"] == "A delegated child is working on this step"


def test_a_worker_running_again_after_an_error_is_working(db):
    step, plan, node = delegated(db)
    _start(db, node)
    _turn(db, "error")
    begin_worker_turn(db, "turn-2", started_at="2000-01-01T00:01:00")

    assert _row(db, step.id)["reason"] == "A delegated child is working on this step"
    assert _worker_liveness(db, plan) == "advancing"


def test_a_completed_last_turn_is_not_stalled(db):
    """Only an error or a hold stalls; a worker between ordinary Turns does not."""
    step, _plan, node = delegated(db)
    _start(db, node)
    _turn(db, "completed")

    assert "stalled" not in str(_row(db, step.id)["reason"])


def test_f1_for_a_held_worker_says_it_resumes_when_its_model_is_ready(db):
    """The held head resumes through catalog recovery, so no 'refused again'."""
    delegated(db)
    _hold_head(db)

    delegation_child_lifecycle.notify_turn_error(
        db, "worker", turn_id="turn-1",
        error="Selected model unavailable: native/model (not_loaded).",
    )

    appended = wakes(db)
    assert len(appended) == 1
    assert str(appended[0]["id"]).startswith("plan-wake:child_provider_refused:")
    content = str(appended[0]["content"])
    assert "native/model (not_loaded)" in content
    assert "delegate_set_model" in content
    assert "resumes on its own once that model is ready again" in content
    assert "may be refused again" not in content


@pytest.mark.parametrize("cause", ["project_cloud_off", "thread_local_only"])
def test_a_locality_held_worker_reads_stalled_for_its_policy(db, cause):
    step, _plan, node = delegated(db)
    _start(db, node)
    _turn(db, "error")
    _hold_head(db, cause)

    assert _row(db, step.id)["reason"] == (
        "The delegated child is stalled: local models only; its model can't run"
    )
    assert _row(db, node.id, WORKER)["reason"] == (
        "Stalled: local models only; its model can't run"
    )


@pytest.mark.parametrize("cause,clause", [
    ("project_cloud_off", "this project uses local models only, so native/model can't run here"),
    ("thread_local_only", "native/model can't run here: this chat is set to local models only;"
                          " it runs once moved to a local model"),
])
def test_f1_for_a_locality_held_worker_names_its_cause_and_the_move(db, cause, clause):
    """Only the person lifts the policy: the wake names moving the thread, never a re-send or a wait."""
    delegated(db)
    _turn(db, "error")
    _hold_head(db, cause)

    delegation_child_lifecycle.notify_turn_error(db, "worker", turn_id="turn-1", error="Held")

    appended = wakes(db)
    assert len(appended) == 1
    assert str(appended[0]["id"]).startswith("plan-wake:child_provider_refused:")
    content = str(appended[0]["content"])
    assert f"is held: {clause}. Its plan and history are intact." in content
    assert "Move the thread to a ready local model from model_catalog (delegate_set_model)." in content
    assert "resumes on its own" not in content
    assert "delegate_send" not in content
