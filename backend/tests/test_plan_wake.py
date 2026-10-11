"""A thread learns about plan work it did not do itself, exactly once.

When someone else closes a task a thread executes, or someone else's write hands
that thread its next task, the thread is told: one input on its own FIFO,
carrying what happened and the same `next` facts a close returns. The thread
that acted is never told — its own result already said so — and a thread that
cannot take input is skipped rather than failing the write that produced the
result.
"""

from __future__ import annotations

import json

import sqlite3
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import admission, service
from plans.boundary import PlanActor, mutate
from tests.plan_execution_helpers import agent_pause

OWNER = PlanActor("owner", "user", "project")
HUMAN = PlanActor(None, "user", "project", kind="user")


def _db(*, owner_model: str | None = "test/model") -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project','Project','t','t')"
    )
    db.execute(
        """INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
           VALUES ('owner','user','project',?,'t','t')""",
        (owner_model,),
    )
    db.commit()
    return db


def _task(db, plan_id: str, title: str):
    node = service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} is proven", db=db,
    )
    db.commit()
    return node


def _update(db, plan_id: str, operation: str, params: dict, *, actor=OWNER) -> dict:
    return mutate(
        plan_id, operation, params, actor=actor, provenance="test.plan_wake",
        mutation_id=uuid.uuid4().hex, db=db,
    ).result


def _revision(db, node_id: str) -> int:
    return int(service._node_row(db, node_id)["revision"])


def _wakes(db, thread_id: str = "owner") -> list[sqlite3.Row]:
    return db.execute(
        "SELECT id,content FROM messages WHERE thread_id=? AND id LIKE 'plan-wake:%' "
        "ORDER BY input_order",
        (thread_id,),
    ).fetchall()


def _submitted(db, plan_id: str, node):
    """Take the task through its owner's own work up to review."""
    admission.activate(
        node.id, expected_revision=_revision(db, node.id), actor_id="owner",
        provenance="test.plan_wake", db=db,
    )
    db.commit()
    _update(db, plan_id, "request_review", {
        "node_id": node.id, "evidence": "The work is done",
        "expected_revision": _revision(db, node.id),
    })
    db.commit()


def test_a_close_by_someone_else_tells_the_executor_once_with_its_next_facts():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        reviewed = _task(db, plan.id, "Draft the brief")
        queued = _task(db, plan.id, "Write the release note")
        _submitted(db, plan.id, reviewed)

        _update(db, plan.id, "accept", {
            "node_id": reviewed.id, "verification_result": "passed",
            "expected_revision": _revision(db, reviewed.id),
        }, actor=HUMAN)
        db.commit()

        wakes = _wakes(db)
        closed = [row for row in wakes if row["id"].startswith("plan-wake:task_closed:")]
        assert len(closed) == 1
        assert closed[0]["id"] == f"plan-wake:task_closed:{reviewed.id}:{_revision(db, reviewed.id)}"
        assert "Draft the brief was closed by another actor." in closed[0]["content"]
        # The same facts a close returns: the executor's slot and open tasks.
        assert '"executor_thread_id": "owner"' in closed[0]["content"]
        assert queued.title in closed[0]["content"]
    finally:
        db.close()


def test_the_thread_that_acted_is_never_told_about_its_own_close():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        own = _task(db, plan.id, "Draft the brief")
        _submitted(db, plan.id, own)

        _update(db, plan.id, "fail", {
            "node_id": own.id, "expected_revision": _revision(db, own.id),
        })
        db.commit()

        assert _wakes(db) == []
    finally:
        db.close()


def test_a_close_that_leaves_nothing_to_act_on_sends_no_wake():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        only = _task(db, plan.id, "Draft the brief")
        _submitted(db, plan.id, only)

        _update(db, plan.id, "accept", {
            "node_id": only.id, "verification_result": "passed",
            "expected_revision": _revision(db, only.id),
        }, actor=HUMAN)
        db.commit()

        # The news is true, and there is nothing to do about it: a Turn started
        # for this would read one line and stop.
        assert _wakes(db) == []
    finally:
        db.close()


def test_a_thread_that_cannot_take_input_is_skipped_not_failed():
    db = _db(owner_model=None)
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        reviewed = _task(db, plan.id, "Draft the brief")
        _submitted(db, plan.id, reviewed)

        result = _update(db, plan.id, "accept", {
            "node_id": reviewed.id, "verification_result": "passed",
            "expected_revision": _revision(db, reviewed.id),
        }, actor=HUMAN)
        db.commit()

        assert result["node"]["state"] == "accepted"
        assert _wakes(db) == []
    finally:
        db.close()


def test_a_review_this_thread_owns_is_work_worth_a_wake():
    """Currency asks the same question liveness asks, so neither drops the other's work.

    A review this thread owns is work it can act on. A rule written over open
    *tasks* answers no -- review is not an open task -- and the wake reporting
    it is refused at append and lost, with the causing write already committed.
    """
    db = _db()
    try:
        from plans import selection_validity, wake_currency

        plan = service.ensure_plan("thread", "owner", db=db)
        reviewed = _task(db, plan.id, "Draft the brief")
        _submitted(db, plan.id, reviewed)

        # The scenario only isolates the defect while no selection is live: a
        # live one made the old rule answer yes for an unrelated reason.
        assert selection_validity.attributed_selection(db, "owner") is None

        assert wake_currency.is_current(
            "owner",
            wake_currency.condition(
                plan_id=plan.id,
                kind="wait_resolved",
                subject_id=reviewed.id,
                revision=_revision(db, reviewed.id),
            ),
            db=db,
        )
    finally:
        db.close()


def test_a_closed_named_node_does_not_fall_through_to_other_actionable_work():
    db = _db()
    try:
        from plans import wake_currency

        plan = service.ensure_plan("thread", "owner", db=db)
        closed = _task(db, plan.id, "Review the proposal")
        other = _task(db, plan.id, "Write the release note")
        _submitted(db, plan.id, closed)
        admission.activate(
            other.id, expected_revision=_revision(db, other.id), actor_id="owner",
            provenance="test.plan_wake", db=db,
        )
        db.commit()
        _update(db, plan.id, "accept", {
            "node_id": closed.id, "verification_result": "passed",
            "expected_revision": _revision(db, closed.id),
        }, actor=HUMAN)
        db.commit()

        assert not wake_currency.is_current(
            "owner",
            wake_currency.condition(
                plan_id=plan.id, kind="ask_released", subject_id=closed.id,
                revision=_revision(db, closed.id), open_node_id=closed.id,
            ),
            db=db,
        )
    finally:
        db.close()


def test_an_admission_defect_fails_the_write_rather_than_losing_the_result():
    """Only the two states a caller tolerates by name are skipped.

    An unavailable or still model-less recipient is skipped, because the plan
    state that gave it work is durable and liveness wakes it later. Anything
    else -- an empty input, a missing socket -- is a defect, and swallowing it
    would drop the result with nothing left to report it.
    """
    db = _db()
    try:
        from infra import input_admission
        from infra.send_types import SendRejected
        from plans import plan_wake

        plan = service.ensure_plan("thread", "owner", db=db)
        node = _task(db, plan.id, "Draft the brief")

        def reject(*_args, **_kwargs):
            raise SendRejected(400, "Message content is empty")

        original = input_admission.accept_thread_input
        input_admission.accept_thread_input = reject
        try:
            with pytest.raises(SendRejected):
                plan_wake.append(
                    db,
                    recipient="owner",
                    kind=plan_wake.TASK_CLOSED,
                    subject_id=node.id,
                    revision=_revision(db, node.id),
                    plan_id=plan.id,
                    content="Draft the brief was closed by another actor.",
                )
        finally:
            input_admission.accept_thread_input = original
    finally:
        db.close()


def test_a_rejection_tells_the_executor_the_revision_is_its_own_and_nothing_else():
    """One story per rejection: not "closed by another actor" plus "your current task".

    The submission is settled and its linked revision is the executor's to
    take up; the executor learns exactly that, once, feedback in hand.
    """
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        reviewed = _task(db, plan.id, "Draft the brief")
        _submitted(db, plan.id, reviewed)

        result = _update(db, plan.id, "reject", {
            "node_id": reviewed.id, "feedback": "Needs the failure modes",
            "expected_revision": _revision(db, reviewed.id),
        }, actor=HUMAN)
        db.commit()

        wakes = _wakes(db)
        assert [w["id"].split(":")[1] for w in wakes] == ["revision_requested"]
        facts = json.loads(wakes[0]["content"].split("\n", 1)[1])
        assert facts["revision"]["node_id"] == result["revision"]["node_id"]
        assert facts["revision"]["feedback"] == "Needs the failure modes"
        assert facts["revision"]["node_id"] in {row["id"] for row in facts["next"]["open_tasks"]}
    finally:
        db.close()


def test_a_selection_made_outside_the_boundary_wrappers_still_wakes_its_executor():
    """Every writer that changes eligibility delivers the same news.

    ``resume_node`` clears its hold through ``holds.clear``, one of the twelve
    callers that discarded the reconciliation result. While the wake was
    emitted by the boundary wrappers instead of by the reconciliation itself,
    those paths selected work and told nobody.
    """
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        task = _task(db, plan.id, "Draft the brief")
        admission.activate(
            task.id, expected_revision=_revision(db, task.id), actor_id="owner",
            provenance="test.plan_wake", db=db,
        )
        db.commit()

        parked = _update(db, plan.id, "pause_node", {
            "node_id": task.id, "reason": "Operator paused the work",
            "expected_revision": _revision(db, task.id),
        }, actor=HUMAN)
        db.commit()
        hold_id = parked["admission"]["causes"][0]["id"]

        before = len(_wakes(db))
        _update(db, plan.id, "resume_node", {
            "node_id": task.id, "hold_id": hold_id,
            "expected_revision": _revision(db, task.id),
        }, actor=HUMAN)
        db.commit()

        selected = [
            w for w in _wakes(db)[before:]
            if w["id"].startswith("plan-wake:work_selected:")
        ]
        assert len(selected) == 1
    finally:
        db.close()


def test_an_executor_resuming_its_own_held_task_is_not_woken_for_its_own_write():
    """The actor exception has to survive emission moving into reconciliation.

    ``reconcile_plan`` knows what it selected but not whose write it is, so a
    path that forgets to pass ``causing_executor`` wakes the very thread that
    just resumed its own work — a Turn to read news it already has.
    """
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        task = _task(db, plan.id, "Draft the brief")
        admission.activate(
            task.id, expected_revision=_revision(db, task.id), actor_id="owner",
            provenance="test.plan_wake", db=db,
        )
        db.commit()

        # A pause the agent placed before agents lost pause stays its own.
        parked = agent_pause(db, task.id, "Paused to take a call")
        hold_id = parked["admission"]["causes"][0]["id"]

        before = len(_wakes(db))
        _update(db, plan.id, "resume_node", {
            "node_id": task.id, "hold_id": hold_id,
            "expected_revision": _revision(db, task.id),
        }, actor=OWNER)
        db.commit()

        assert _wakes(db)[before:] == []
    finally:
        db.close()


def test_a_concurrent_mutations_wake_is_never_claimed_by_this_write(monkeypatch):
    """Recipients are read inside the write, where the rows can only be ours.

    A rowid floor records when a row was written, never by whom. Read after
    commit, another mutation's wake sitting above the same floor is
    indistinguishable from this write's own, so both writes drive that thread
    and the drive-once ownership is not actually established.
    """
    db = _db()
    try:
        from plans import notifications

        db.execute(
            """INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
               VALUES ('intruder','user','project','test/model','t','t')"""
        )
        plan = service.ensure_plan("thread", "owner", db=db)
        reviewed = _task(db, plan.id, "Draft the brief")
        _submitted(db, plan.id, reviewed)
        db.commit()

        original = notifications.prepare_after_commit

        def another_write_commits_its_own_wake(plan_ids, *, db):
            db.execute(
                "INSERT INTO messages (id,thread_id,role,content,created_at) "
                "VALUES ('plan-wake:task_closed:foreign:1','intruder','user','x','t')"
            )
            return original(plan_ids, db=db)

        monkeypatch.setattr(
            notifications, "prepare_after_commit", another_write_commits_its_own_wake,
        )
        deferred = mutate(
            plan.id, "accept", {
                "node_id": reviewed.id, "verification_result": "passed",
                "expected_revision": _revision(db, reviewed.id),
            },
            actor=HUMAN, provenance="test.plan_wake",
            mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()

        assert "intruder" not in deferred.drives
    finally:
        db.close()


def test_a_plan_wake_is_a_system_context_event_like_every_routed_event():
    """One shape for every event input: a system row the transcript shows as a chip."""
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        reviewed = _task(db, plan.id, "Draft the brief")
        _submitted(db, plan.id, reviewed)
        _update(db, plan.id, "reject", {
            "node_id": reviewed.id, "feedback": "Needs the failure modes",
            "expected_revision": _revision(db, reviewed.id),
        }, actor=HUMAN)
        db.commit()

        rows = db.execute(
            "SELECT role, content, metadata FROM messages WHERE id LIKE 'plan-wake:%' ORDER BY seq"
        ).fetchall()
        assert rows, "the owner was handed its revision"
        metadata = json.loads(rows[0]["metadata"])
        assert rows[0]["role"] == "system"
        assert (metadata["type"], metadata["event_disposition"]) == ("context_event", "wake")
        assert metadata["event_type"] == "plan.revision_requested"
        # The chip has its own short line; the agent's line stays in its content.
        assert metadata["summary"] == "Returned work was rejected"
        assert rows[0]["content"].startswith("[CORSIA plan]")
    finally:
        db.close()
