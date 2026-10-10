"""Waiting work is quiet, and every wait is one admission cause.

- A decision raised to the user is its source task's wait: the task gives up its
  selection, the next task takes the slot, and the thread's liveness stops
  counting the task until the answer lands.
- A child's draft waits on the parent tier that reviews it. Root work has no
  higher tier, so its author's own draft is no wait at all.
- A delegated step waits while its child's plan moves, and is blocked instead
  when that child thread is gone.
- A hold and a watch name a real wait before a task starts and while its review
  is outstanding, not only mid-execution.
- A settled watch can be armed again, its terminating event is part of what
  makes it one wait rather than another, and settling it ends its expiry
  continuation in the same write.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import admission, decisions, mutations, projections, service
from plans.boundary import PlanActor, mutate
from tests.plan_ask_helpers import with_actionable_user_ask
from tests.plan_execution_helpers import agent_pause

OWNER = PlanActor("owner", "user", "project")
CHILD = PlanActor("child", "user", "project")
HUMAN = PlanActor(None, "user", None, kind="user")
_OPTIONS = [
    {"id": "A", "label": "Ship it", "detail": "Take the shorter path."},
    {"id": "B", "label": "Split it", "detail": "Take the safer path."},
]


def _db() -> sqlite3.Connection:
    """A project with a plan owner and the child thread it delegates to."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project','Project','t','t')"
    )
    for thread_id, spawned_by in (("owner", None), ("child", "owner")):
        db.execute(
            """INSERT INTO threads
               (id,user_id,project_id,model,spawned_by,created_at,updated_at)
               VALUES (?,'user','project','test/model',?,'t','t')""",
            (thread_id, spawned_by),
        )
    db.commit()
    return db


def _task(db, plan_id: str, title: str, **kwargs):
    node = service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} is proven", db=db, **kwargs,
    )
    db.commit()
    return node


def _activate(db, node_id: str, actor_id: str = "owner") -> None:
    admission.activate(
        node_id, expected_revision=service._node_row(db, node_id)["revision"],
        actor_id=actor_id, provenance="test.durable_waits", db=db,
    )
    db.commit()


def _update(db, plan_id: str, operation: str, params: dict, *, actor=OWNER) -> dict:
    params = with_actionable_user_ask(db, plan_id, operation, params, actor)
    return mutate(
        plan_id, operation, params, actor=actor, provenance="test.durable_waits",
        mutation_id=uuid.uuid4().hex, db=db,
    ).result


def _causes(db, node_id: str) -> list[dict]:
    return admission.project(node_id, db=db)["causes"]


def _raised(db, plan, source, *, actor=OWNER) -> dict:
    """Draft, review and raise one decision on a task, as the tiers do."""
    decision = decisions.draft(
        plan.id, source_node_id=source.id, mutation_id=uuid.uuid4().hex,
        actor=actor, provenance="test.durable_waits", db=db,
        title="Which path?", context="Two ways to finish this.",
        tradeoff="Shorter against safer.", recommendation="A",
        rationale="The shorter path is reversible.", options=_OPTIONS,
        other_enabled=True,
    ).result["decision"]
    for operation in (decisions.parent_review, decisions.raise_to_user):
        decision = operation(
            decision["id"], expected_revision=decision["revision"],
            mutation_id=uuid.uuid4().hex, actor=actor,
            provenance="test.durable_waits", db=db,
        ).result["decision"]
    db.commit()
    return decision


def test_a_raised_decision_waits_and_hands_the_slot_to_the_next_task():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        deciding = _task(db, plan.id, "Name the feature")
        queued = _task(db, plan.id, "Write the release note")
        _activate(db, deciding.id)

        decision = _raised(db, plan, deciding)

        projection = admission.project(deciding.id, db=db)
        assert projection["condition"] == "waiting"
        assert projection["causes"][0]["reason_key"] == "decision_raised"
        assert projection["causes"][0]["owner_kind"] == "user"
        # The slot is free for the executor's next task, which stays the
        # agent's to start: waiting never auto-activates queued work.
        assert projection["selection"] is None
        assert admission.project(queued.id, db=db)["condition"] == "ready"
        signal = projections.liveness_signal("owner", db=db)
        assert deciding.id not in signal.node_ids
        assert queued.id in signal.node_ids

        decisions.answer(
            decision["id"], option_id="A", other_text=None,
            expected_revision=decision["revision"], mutation_id=uuid.uuid4().hex,
            actor=HUMAN, provenance="test.durable_waits", db=db,
        )
        db.commit()

        assert _causes(db, deciding.id) == []
    finally:
        db.close()


def test_a_raised_decision_the_user_can_no_longer_answer_stops_being_a_wait():
    from registry import attention

    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        deciding = _task(db, plan.id, "Name the feature")
        _activate(db, deciding.id)
        decision = _raised(db, plan, deciding)
        assert _causes(db, deciding.id)[0]["reason_key"] == "decision_raised"

        from infra.database import write_transaction
        with write_transaction(db, scope="test_attention_setup"):
            attention.clear_attention(
                decision["artifact_id"], outcome="dismiss",
                reason="cleared out of band", db=db,
            )
        db.commit()

        assert _causes(db, deciding.id) == []
    finally:
        db.close()


def test_a_watch_for_a_different_event_is_its_own_wait():
    from tests.plan_scene import _artifact

    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        watched = _task(db, plan.id, "Await CI")
        # Bound to an artifact, so each expected_event is one that can arrive
        # and end its own wait.
        arm = {
            "node_id": watched.id,
            "condition": {"type": "event", "value": "ci.complete"},
            "artifact_id": _artifact(db, external_id="owner/repo#1", owner="owner"),
        }

        first = _update(db, plan.id, "arm_watch", {
            **arm, "expected_event": "github.check_suite.completed",
            "expected_revision": service._node_row(db, watched.id)["revision"],
        })["watch"]["id"]
        again = _update(db, plan.id, "arm_watch", {
            **arm, "expected_event": "github.check_suite.completed",
            "expected_revision": service._node_row(db, watched.id)["revision"],
        })["watch"]["id"]
        other = _update(db, plan.id, "arm_watch", {
            **arm, "expected_event": "github.deployment_status",
            "expected_revision": service._node_row(db, watched.id)["revision"],
        })["watch"]["id"]
        db.commit()

        # The same wait twice is one watch; a different terminating event is a
        # different wait and must not silently reuse the first one.
        assert again == first
        assert other != first
    finally:
        db.close()


def test_a_childs_draft_waits_on_the_parent_tier_and_a_root_draft_does_not():
    db = _db()
    try:
        child_plan = service.ensure_plan("thread", "child", db=db)
        child_task = _task(db, child_plan.id, "Choose the source set")
        _activate(db, child_task.id, actor_id="child")
        owner_plan = service.ensure_plan("thread", "owner", db=db)
        owner_task = _task(db, owner_plan.id, "Choose the schedule")
        _activate(db, owner_task.id)

        for plan, source, actor in (
            (child_plan, child_task, CHILD), (owner_plan, owner_task, OWNER),
        ):
            decisions.draft(
                plan.id, source_node_id=source.id, mutation_id=uuid.uuid4().hex,
                actor=actor, provenance="test.durable_waits", db=db,
                title="Which path?", context="Two ways to finish this.",
                tradeoff="Shorter against safer.", recommendation="A",
                rationale="The shorter path is reversible.", options=_OPTIONS,
                other_enabled=True,
            )
        db.commit()

        child_cause = _causes(db, child_task.id)[0]
        assert (child_cause["reason_key"], child_cause["owner_id"]) == (
            "decision_review", "owner",
        )
        # Root work has no higher tier: its author must act on its own draft.
        assert _causes(db, owner_task.id) == []
    finally:
        db.close()


def test_a_delegated_step_waits_while_its_child_works_and_blocks_when_it_is_gone():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        step = _task(db, plan.id, "Delegated research", spawn_kind="claude-code")
        queued = _task(db, plan.id, "Write the release note")
        _activate(db, step.id)
        child_plan = service.ensure_plan("thread", "child", db=db)
        child_task = _task(db, child_plan.id, "Gather sources")
        mutations.attach_spawned_thread(step.id, "child", db=db)
        _activate(db, child_task.id, actor_id="child")

        projection = admission.project(step.id, db=db)
        assert projection["condition"] == "waiting"
        assert projection["causes"][0]["reason_key"] == "delegated_child"

        # The child started after the attachment, so the parent gives the step's
        # slot back at its own next write, as it does for any wait it acquires.
        admission.reconcile_plan(
            plan.id, actor_id="owner", provenance="test.durable_waits", db=db,
        )
        db.commit()

        assert admission.project(step.id, db=db)["selection"] is None
        assert admission.project(queued.id, db=db)["condition"] == "ready"
        assert step.id not in projections.liveness_signal("owner", db=db).node_ids

        db.execute(
            "UPDATE threads SET lifecycle_state='archived', archived_reason='user' "
            "WHERE id='child'"
        )
        db.commit()

        blocked = admission.project(step.id, db=db)
        assert blocked["condition"] == "blocked"
        assert blocked["causes"][0]["reason_key"] == "delegated_child_unavailable"
    finally:
        db.close()


def test_a_task_waits_on_a_hold_or_a_watch_before_it_ever_starts():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        held = _task(db, plan.id, "Await legal")
        watched = _task(db, plan.id, "Await CI")

        # Only the user's own blocker is a hold; any other is a deadline wait.
        _update(db, plan.id, "block_node", {
            "node_id": held.id, "reason_key": "legal", "reason": "Legal's sign-off",
            "owner_kind": "user", "owner_id": None,
            "expected_revision": service._node_row(db, held.id)["revision"],
        })
        _update(db, plan.id, "arm_watch", {
            "node_id": watched.id,
            "condition": {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
            "expected_event": "ci.complete",
            "expected_revision": service._node_row(db, watched.id)["revision"],
        })
        db.commit()

        assert service._node_row(db, held.id)["state"] == "pending"
        assert service._node_row(db, watched.id)["state"] == "pending"
        assert admission.project(held.id, db=db)["condition"] == "blocked"
        assert admission.project(watched.id, db=db)["condition"] == "waiting"
        assert projections.liveness_signal("owner", db=db).count == 0
    finally:
        db.close()


def test_a_hold_placed_during_review_keeps_the_review_from_being_accepted():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        reviewed = _task(db, plan.id, "Draft the brief")
        _activate(db, reviewed.id)
        _update(db, plan.id, "request_review", {
            "node_id": reviewed.id, "evidence": "The brief is written",
            "expected_revision": service._node_row(db, reviewed.id)["revision"],
        })
        agent_pause(db, reviewed.id, "Hold for the launch decision")

        assert _causes(db, reviewed.id)[0]["kind"] == "paused"
        with pytest.raises(service.PlanError):
            _update(db, plan.id, "accept", {
                "node_id": reviewed.id, "verification_result": "passed",
                "expected_revision": service._node_row(db, reviewed.id)["revision"],
            })
        assert service._node_row(db, reviewed.id)["state"] == "review_requested"
    finally:
        db.close()


def test_resolve_watch_records_bounded_optional_reason_and_evidence():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        watched = _task(db, plan.id, "Await CI")
        armed = _update(db, plan.id, "arm_watch", {
            "node_id": watched.id,
            "condition": {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
            "expected_event": "ci.complete",
            "expected_revision": service._node_row(db, watched.id)["revision"],
        })["watch"]

        _update(db, plan.id, "resolve_watch", {
            "node_id": watched.id, "watch_id": armed["id"],
            "reason": "Finished waiting", "evidence": "x" * 2_000,
            "expected_revision": service._node_row(db, watched.id)["revision"],
        })

        event = db.execute(
            "SELECT payload_json FROM plan_node_events WHERE node_id=? "
            "AND event='watch_resolved' ORDER BY rowid DESC LIMIT 1", (watched.id,),
        ).fetchone()
        payload = json.loads(event["payload_json"])
        assert payload["reason"] == "Finished waiting"
        assert payload["evidence"] != "x" * 2_000
        assert len(json.dumps(payload["evidence"])) <= 480
    finally:
        db.close()


def test_rearming_supersedes_only_past_armed_deadlines():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        watched = _task(db, plan.id, "Await CI")
        first = _update(db, plan.id, "arm_watch", {
            "node_id": watched.id,
            "condition": {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
            "expected_event": "ci.complete",
            "expected_revision": service._node_row(db, watched.id)["revision"],
        })["watch"]["id"]
        db.execute(
            "UPDATE expected_event_watches SET condition_value='2000-01-01T00:00:00+00:00' WHERE id=?",
            (first,),
        )
        second = _update(db, plan.id, "arm_watch", {
            "node_id": watched.id,
            "condition": {"type": "deadline", "value": "2099-02-01T00:00:00+00:00"},
            "expected_event": "ci.complete",
            "expected_revision": service._node_row(db, watched.id)["revision"],
        })["watch"]["id"]

        statuses = {
            row["id"]: row["status"] for row in db.execute(
                "SELECT id,status FROM expected_event_watches WHERE plan_node_id=?", (watched.id,),
            )
        }
        event = db.execute(
            "SELECT payload_json FROM plan_node_events WHERE node_id=? "
            "AND event='watch_armed' ORDER BY rowid DESC LIMIT 1", (watched.id,),
        ).fetchone()
        assert statuses == {first: "resolved", second: "armed"}
        assert json.loads(event["payload_json"])["superseded_watch_ids"] == [first]
    finally:
        db.close()


def test_rearming_keeps_future_and_expired_deadline_rows():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        watched = _task(db, plan.id, "Await CI")
        first = _update(db, plan.id, "arm_watch", {
            "node_id": watched.id,
            "condition": {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
            "expected_event": "ci.complete",
            "expected_revision": service._node_row(db, watched.id)["revision"],
        })["watch"]["id"]
        db.execute(
            "UPDATE expected_event_watches SET status='expired' WHERE id=?", (first,),
        )
        second = _update(db, plan.id, "arm_watch", {
            "node_id": watched.id,
            "condition": {"type": "deadline", "value": "2099-02-01T00:00:00+00:00"},
            "expected_event": "ci.complete",
            "expected_revision": service._node_row(db, watched.id)["revision"],
        })["watch"]["id"]
        third = _update(db, plan.id, "arm_watch", {
            "node_id": watched.id,
            "condition": {"type": "deadline", "value": "2099-03-01T00:00:00+00:00"},
            "expected_event": "ci.complete",
            "expected_revision": service._node_row(db, watched.id)["revision"],
        })["watch"]["id"]

        statuses = {
            row["id"]: row["status"] for row in db.execute(
                "SELECT id,status FROM expected_event_watches WHERE plan_node_id=?", (watched.id,),
            )
        }
        assert statuses == {first: "expired", second: "armed", third: "armed"}
    finally:
        db.close()


def test_a_settled_watch_can_be_armed_again_and_ends_its_expiry():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        watched = _task(db, plan.id, "Await CI")
        arm = {
            "node_id": watched.id,
            "condition": {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
            "expected_event": "ci.complete",
        }

        first = _update(db, plan.id, "arm_watch", {
            **arm, "expected_revision": service._node_row(db, watched.id)["revision"],
        })["watch"]["id"]
        db.execute(
            """INSERT INTO continuation_triggers
               (id,kind,thread_id,scope_id,subject_id,dedupe_key,state,created_at,updated_at)
               VALUES ('t2','t2_watch_expiry','owner','project',?,'dedupe','deferred','t','t')""",
            (first,),
        )
        _update(db, plan.id, "resolve_watch", {
            "node_id": watched.id, "watch_id": first,
            "expected_revision": service._node_row(db, watched.id)["revision"],
        })
        second = _update(db, plan.id, "arm_watch", {
            **arm, "expected_revision": service._node_row(db, watched.id)["revision"],
        })["watch"]["id"]
        db.commit()

        assert second != first
        assert admission.project(watched.id, db=db)["condition"] == "waiting"
        expiry = db.execute(
            "SELECT state,reason FROM continuation_triggers WHERE id='t2'",
        ).fetchone()
        assert (expiry["state"], expiry["reason"]) == ("cancelled", "watch_resolved")
    finally:
        db.close()
