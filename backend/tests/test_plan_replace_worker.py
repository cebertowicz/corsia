"""Matrix row F6 and debt 8: one detach beside attach, and replacing a worker.

A parent that stands a worker down hands its step to a replacement in one
write: the replacement is attached even mid-work, the old worker's open steps
close as "replaced", and the old attachment is detached. From then on only the
replacement can wake the parent about the step or hold it waiting. The old
worker's own live workers are named for the caller, not moved.
"""

from __future__ import annotations

from automation.turn_end_effects import PendingTurnEndEffects
from automation import plan_liveness_terminal
import json
from datetime import datetime, timezone

import pytest

from automation import plan_liveness
from plans import admission, delegation, delegation_child_lifecycle, mutations, service
from plans.projections import liveness_signal
from tests.child_parent_notice_helpers import db, delegated, wakes
from tests.plan_replace_worker_helpers import (
    events as _events,
    replace as _replace,
    replaced_notices as _replaced_notices,
    row as _row,
    started_replacement as _started_replacement,
    thread as _thread,
)

__all__ = ["db"]


# --- Debt 8: detach is an operation beside attach ----------------------------


def test_detach_clears_the_worker_with_one_event_and_a_revision(db):
    step, plan, _node = delegated(db)
    before = _row(db, step.id)["revision"]

    detached = delegation.detach_spawned_thread(
        step.id,
        reason="stood down",
        expected_revision=before,
        actor_id="parent",
        db=db,
    )

    assert detached.spawned_thread_id is None
    assert detached.revision == before + 1
    assert _events(db, step.id, "spawned_thread_detached") == [
        {
            "spawned_thread_id": "worker",
            "child_plan_id": plan.id,
            "reason": "stood down",
        },
    ]


def test_detach_refuses_a_stale_revision(db):
    step, _plan, _node = delegated(db)

    with pytest.raises(service.PlanError):
        delegation.detach_spawned_thread(
            step.id,
            reason="stood down",
            expected_revision=0,
            db=db,
        )
    assert _row(db, step.id)["spawned_thread_id"] == "worker"


def test_detach_is_a_no_op_when_nothing_is_attached(db):
    step, _plan, _node = delegated(db)
    delegation.detach_spawned_thread(step.id, reason="first", db=db)
    revision = _row(db, step.id)["revision"]

    delegation.detach_spawned_thread(step.id, reason="again", db=db)

    assert _row(db, step.id)["revision"] == revision
    assert len(_events(db, step.id, "spawned_thread_detached")) == 1


def test_a_gone_child_is_released_through_detach(db):
    """Slice 5's dead-child release now writes the same event as every detach."""
    step, plan, _node = delegated(db)

    delegation_child_lifecycle.on_child_thread_ended(db, "worker", reason="purged")

    assert _events(db, step.id, "spawned_thread_detached") == [
        {"spawned_thread_id": "worker", "child_plan_id": plan.id, "reason": "purged"},
    ]


# --- F6: replace a worker ----------------------------------------------------


def test_replace_hands_the_step_to_a_replacement_that_already_started(db):
    step, _old_plan, old_node = delegated(db)
    service.transition_node(old_node.id, "active", actor_id="worker", db=db)
    _new_plan, new_node = _started_replacement(db)

    _replace(db, step.id, "replacement")

    assert _row(db, step.id)["spawned_thread_id"] == "replacement"
    assert _row(db, step.id)["description"] is None
    closed = _row(db, old_node.id)
    assert closed["state"] == "closed"
    assert closed["archived_at"] is None
    transition = db.execute(
        """SELECT payload_json FROM plan_node_events
           WHERE node_id=? AND new_state='closed'""",
        (old_node.id,),
    ).fetchone()
    assert json.loads(transition["payload_json"])["reason"] == "replaced"
    assert [
        e["spawned_thread_id"] for e in _events(db, step.id, "spawned_thread_detached")
    ] == [
        "worker",
    ]
    # Work the replacement already ran keeps its own commitment; nothing split.
    assert _row(db, new_node.id)["commitment_id"] == new_node.id


def test_after_replace_only_the_replacement_can_hold_or_wake_the_step(db):
    step, old_plan, _old_node = delegated(db)
    _started_replacement(db)
    _replace(db, step.id, "replacement")
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES ('old-turn','worker',0,'native/model','running',?,'boot-1')""",
        (now,),
    )
    before = len(wakes(db))

    # The old worker keeps going: a new step, a paused plan, an errored Turn.
    service.add_node(
        old_plan.id, kind="task", title="Stray", acceptance_criteria="-", db=db
    )
    plan_liveness_terminal.on_thread_turn_complete(
        "worker",
        outcome="error",
        error="boom",
        turn_id="old-turn",
        db=db,
        effects=PendingTurnEndEffects(),
    )

    assert wakes(db)[before:] == []
    causes = admission.project(step.id, db=db)["causes"]
    assert [cause["child_thread_id"] for cause in causes] == ["replacement"]
    assert liveness_signal("parent", db=db).count == 0


def test_a_wake_the_old_worker_queued_before_replace_goes_stale(db):
    """PR #2011 review: currency names the worker, not just the step."""
    from plans import wake_currency

    step, _plan, _node = delegated(db)
    _started_replacement(db)

    def queued_by(worker: str) -> dict:
        return wake_currency.condition(
            plan_id=step.plan_id,
            kind="child_returned",
            subject_id=step.id,
            revision=1,
            delegated_step_id=step.id,
            delegated_worker=worker,
        )

    assert wake_currency.is_current("parent", queued_by("worker"), db=db)
    _replace(db, step.id, "replacement")

    assert not wake_currency.is_current("parent", queued_by("worker"), db=db)
    assert wake_currency.is_current("parent", queued_by("replacement"), db=db)


def test_the_old_workers_return_is_not_the_replacements_round(db):
    """PR #2011 review: a round is measured from this worker's own harvest."""
    from plans import delegation
    from tests.plan_review_helpers import submit_review_evidence

    step, _old_plan, old_node = delegated(db)
    service.transition_node(old_node.id, "active", actor_id="worker", db=db)
    submit_review_evidence(old_node.id, db=db, actor_id="worker")
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [step.id]
    _thread(db, "replacement", "parent")
    service.ensure_plan("thread", "replacement", db=db)
    _replace(db, step.id, "replacement")
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES ('new-turn','replacement',0,'native/model','completed',?,'boot-1')""",
        (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),),
    )
    before = len(wakes(db))

    plan_liveness_terminal.on_thread_turn_complete(
        "replacement",
        db=db,
        effects=PendingTurnEndEffects(),
    )

    assert [
        row["id"]
        for row in wakes(db)[before:]
        if "child_round_returned" in str(row["id"])
    ] == []


def test_a_replacement_after_review_returns_and_wakes_the_parent(db):
    """#2310: replacing a harvested step takes it out of review, so the
    replacement's return is harvested and wakes the parent like the first."""
    from tests.plan_review_helpers import submit_review_evidence

    step, _old_plan, old_node = delegated(db)
    service.transition_node(old_node.id, "active", actor_id="worker", db=db)
    submit_review_evidence(old_node.id, db=db, actor_id="worker")
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [step.id]
    _new_plan, new_node = _started_replacement(db)

    _replace(db, step.id, "replacement")

    assert _row(db, step.id)["state"] == "active"
    before = len(wakes(db))
    submit_review_evidence(new_node.id, db=db, actor_id="replacement")
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [step.id]
    assert _row(db, step.id)["state"] == "review_requested"
    returned = wakes(db)[before:]
    assert len(returned) == 1
    assert "Fix it properly" in str(returned[0]["content"])


def test_a_refused_replacement_leaves_the_old_worker_whole(db):
    """Close and detach run before attach; a refused attach must undo both."""
    step, _old_plan, old_node = delegated(db)
    service.transition_node(old_node.id, "active", actor_id="worker", db=db)
    _thread(db, "stranger", "grandparent")  # not spawned by the plan's owner

    with pytest.raises(service.PlanError):
        _replace(db, step.id, "stranger")

    assert _row(db, step.id)["spawned_thread_id"] == "worker"
    assert _row(db, old_node.id)["state"] == "active"
    assert _row(db, old_node.id)["archived_at"] is None
    assert _events(db, step.id, "spawned_thread_detached") == []
    assert _replaced_notices(db, "worker") == []


def test_replace_shows_the_replacement_as_the_steps_worker(db):
    """The pane reads the node's child from its attachment, never its prose."""
    from plans import rollup

    step, _plan, _node = delegated(db)
    _started_replacement(db)

    _replace(db, step.id, "replacement")

    parent_plan = service.ensure_plan("thread", "parent", db=db)
    tree = rollup.rollup_thread(parent_plan.id, db=db, child_depth=0)
    node = next(item for item in tree.nodes if item["id"] == step.id)
    assert node["child_plan"].plan["owner_id"] == "replacement"


def test_attach_over_a_different_live_worker_names_replace(db):
    step, _plan, _node = delegated(db)
    _started_replacement(db)

    with pytest.raises(service.PlanInputError, match="replace_spawned_thread"):
        mutations.attach_spawned_thread(step.id, "replacement", db=db)
    assert _row(db, step.id)["spawned_thread_id"] == "worker"
