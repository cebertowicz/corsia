"""A parent that owes the verdict on a returned worker is told what to do (matrix row G-owed).

The park result instructs at the moment the parent waits on a returned step,
and one reminder follows when the verdict is still owed 30 minutes later.
"""

from __future__ import annotations

import uuid

from automation.continuation.sources.owed_verdict import OwedVerdictSource
from plans import (
    boundary, delegation, delegation_harvest, delegation_owed_verdict, mutations, public, service,
)
from plans.boundary import PlanActor
from tests.child_parent_notice_helpers import db, delegated, wakes
from tests.plan_review_helpers import submit_review_evidence

__all__ = ["db"]

PARENT = PlanActor("parent", "user-1", "project-1")
NOW = "2100-01-01T00:00:00+00:00"


def _harvested(db):
    """The worker returned its only step and the harvest put the parent's step in review."""
    step, plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    submit_review_evidence(node.id, db=db, actor_id="worker")
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [step.id]
    return step, plan, node


def _age_return(db, node_id: str, at: str = "2099-12-31T23:00:00+00:00") -> None:
    """Move the harvest's review transition to ``at`` (events are append-only)."""
    db.execute("DROP TRIGGER IF EXISTS prevent_plan_node_event_update")
    db.execute(
        """UPDATE plan_node_events SET created_at=?
           WHERE node_id=? AND provenance='plan.delegation_harvest'
             AND event='state_transition' AND new_state='review_requested'""",
        (at, node_id),
    )


def _mutate(db, plan_id, operation, params):
    return boundary.mutate(
        plan_id, operation, params, actor=PARENT, provenance="tool.plans",
        mutation_id=uuid.uuid4().hex, db=db,
    ).result


def _revision(db, node_id) -> int:
    return int(service._node_row(db, node_id)["revision"])


def _watch(db, step):
    return _mutate(db, step.plan_id, "arm_watch", {
        "node_id": step.id, "expected_revision": _revision(db, step.id),
        "condition": {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
        "reason": "Hold until the PR merges",
    })


def _settle(db, step) -> None:
    submit_review_evidence(step.id, db=db, actor_id="parent")
    service.transition_node(
        step.id, "accepted", verification_result="passed",
        verified_by="parent", actor_id="parent", db=db,
    )


def _sweep(db, now: str = NOW) -> list[str]:
    return OwedVerdictSource().prepare(now, db=db)


def _owed_wakes(db):
    return [w for w in wakes(db) if ":verdict_owed:" in str(w["id"])]


def test_arm_watch_on_a_returned_step_carries_the_instruction(db):
    step, _plan, _node = _harvested(db)

    result = _watch(db, step)

    owed = result["verdict_owed"]
    assert f"({step.id})" in owed and "Delegate work" in owed
    assert "accept_with_follow_up" in owed and "reject it with feedback" in owed
    assert len(owed.encode()) < 400
    assert service._node_row(db, step.id)["state"] == "review_requested"


def test_non_user_block_on_a_returned_step_carries_the_instruction(db):
    step, _plan, _node = _harvested(db)

    result = _mutate(db, step.plan_id, "block_node", {
        "node_id": step.id, "expected_revision": _revision(db, step.id),
        "owner_kind": "external", "reason_key": "pr_merge", "reason": "Waiting for the merge",
    })

    assert "verdict_owed" in result


def test_user_block_on_a_returned_step_carries_no_instruction(db):
    step, _plan, _node = _harvested(db)

    result = boundary.mutate(
        step.plan_id, "block_node", {
            "node_id": step.id, "expected_revision": _revision(db, step.id),
            "owner_kind": "user", "reason_key": "decision", "reason": "Needs the user",
        },
        actor=PlanActor(None, "user-1", "project-1", kind="user"),
        provenance="tool.plans", mutation_id=uuid.uuid4().hex, db=db,
    ).result

    assert "verdict_owed" not in result


def test_ordinary_task_and_owners_own_review_carry_no_instruction(db):
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    plain = service.add_node(
        parent_plan.id, kind="task", title="Plain", acceptance_criteria="ok", db=db,
    )
    service.transition_node(plain.id, "active", actor_id="parent", db=db)
    assert "verdict_owed" not in _watch(db, plain)

    own = service.add_node(
        parent_plan.id, kind="task", title="Own review", acceptance_criteria="ok",
        spawn_kind="claude-code", db=db,
    )
    service.transition_node(own.id, "active", actor_id="parent", db=db)
    from plans import mutations

    mutations.attach_spawned_thread(own.id, "worker", db=db)
    submit_review_evidence(own.id, db=db, actor_id="parent")
    assert delegation_harvest.awaiting_owner_verdict(db, service._node_row(db, own.id)) is False
    assert "verdict_owed" not in _watch(db, own)


def test_reminder_is_written_once_after_thirty_minutes(db):
    step, _plan, node = _harvested(db)
    _age_return(db, step.id)
    before = len(_owed_wakes(db))

    assert _sweep(db, "2099-12-31T23:29:00+00:00") == []
    assert _sweep(db, "2099-12-31T23:31:00+00:00") == ["parent"]
    assert _sweep(db) == []

    appended = _owed_wakes(db)[before:]
    assert len(appended) == 1
    content = str(appended[0]["content"])
    assert "Delegate work" in content and "accept_with_follow_up" in content
    assert "30 minutes ago" in content


def test_reminder_fires_even_when_the_parent_armed_a_watch_on_the_step(db):
    step, _plan, _node = _harvested(db)
    _watch(db, step)
    _age_return(db, step.id)

    assert _sweep(db) == ["parent"]


def test_no_reminder_for_a_paused_plan_until_it_resumes(db):
    step, _plan, _node = _harvested(db)
    _age_return(db, step.id)
    db.execute("UPDATE plans SET state='paused' WHERE owner_id='parent'")

    assert _sweep(db) == []
    db.execute("UPDATE plans SET state='active' WHERE owner_id='parent'")
    assert _sweep(db) == ["parent"]


def test_no_reminder_once_the_step_is_accepted_or_rejected(db):
    step, _plan, _node = _harvested(db)
    _age_return(db, step.id)
    _settle(db, step)

    assert _sweep(db) == []


def test_no_reminder_once_the_step_is_rejected(db):
    step, _plan, _node = _harvested(db)
    _age_return(db, step.id)
    _mutate(db, step.plan_id, "reject", {
        "node_id": step.id, "expected_revision": _revision(db, step.id),
        "feedback": "Fix the edge case",
    })

    assert _sweep(db) == []


def test_second_return_round_gets_its_own_reminder(db):
    step, _plan, _node = _harvested(db)
    _age_return(db, step.id)
    assert _sweep(db) == ["parent"]

    _mutate(db, step.plan_id, "reject", {
        "node_id": step.id, "expected_revision": _revision(db, step.id),
        "feedback": "Fix the edge case",
    })
    assert _sweep(db) == []
    # A rejection opens a revision step for the worker's revision of the work.
    revision_step = db.execute(
        "SELECT id FROM plan_nodes WHERE plan_id=? AND state='pending'", (step.plan_id,),
    ).fetchone()["id"]
    revise = db.execute("SELECT id FROM plan_nodes WHERE title LIKE 'Revise:%'").fetchone()["id"]
    service.transition_node(revision_step, "active", db=db)
    service.transition_node(revise, "active", actor_id="worker", db=db)
    submit_review_evidence(revise, db=db, actor_id="worker")
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [
        revision_step,
    ]
    assert service._node_row(db, revision_step)["state"] == "review_requested"

    _age_return(db, revision_step, "2099-12-31T23:50:00+00:00")
    assert _sweep(db) == []  # returned ten minutes ago: not yet owed
    _age_return(db, revision_step, "2099-12-31T23:10:00+00:00")
    assert _sweep(db) == ["parent"]
    assert len(_owed_wakes(db)) == 2


def test_sweep_with_nothing_owed_takes_no_writer(db, monkeypatch):
    opened = []
    real = public.plan_transaction
    monkeypatch.setattr(
        public, "plan_transaction", lambda d: (opened.append(1), real(d))[1],
    )
    _harvested(db)  # returned just now: not yet owed

    assert _sweep(db, "2000-01-01T00:00:00+00:00") == []
    assert opened == []


def test_runner_sweep_without_a_connection_reads_the_pooled_one(db, monkeypatch):
    from automation.continuation.sources import owed_verdict
    from infra import database

    pooled = database._NoOpClose(db)
    monkeypatch.setattr(owed_verdict, "get_db", lambda: pooled)
    monkeypatch.setattr(database, "get_db", lambda: pooled)
    step, _plan, _node = _harvested(db)
    _age_return(db, step.id)
    db.commit()

    assert _sweep(None) == ["parent"]
    assert len(_owed_wakes(db)) == 1


def test_write_rechecks_the_candidates_it_was_given(db):
    step, _plan, _node = _harvested(db)
    _age_return(db, step.id)
    candidates = delegation_owed_verdict.owed_verdicts(db, now=NOW)
    assert [row["node_id"] for row in candidates] == [step.id]
    _settle(db, step)

    with public.plan_transaction(db) as connection:
        delegation_owed_verdict.notify_owed_verdicts(connection, now=NOW, node_ids=[step.id])

    assert _owed_wakes(db) == []


def test_latest_review_event_lookup_uses_the_node_index(db):
    plan = db.execute(
        """EXPLAIN QUERY PLAN SELECT id,event,provenance,created_at FROM plan_node_events
           WHERE node_id=?
             AND ((event='state_transition' AND new_state='review_requested')
                  OR event='review_evidence_submitted')
           ORDER BY created_at DESC,rowid DESC LIMIT 1""",
        ("n",),
    ).fetchall()
    detail = " ".join(str(row["detail"]) for row in plan)
    assert "SEARCH" in detail and "idx_plan_node_events_node" in detail
    assert "SCAN" not in detail


def _more_harvested(db, index: int):
    """Another parent step whose own worker returned and was harvested."""
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    step = service.add_node(
        parent_plan.id, kind="task", title=f"Delegate more {index}",
        acceptance_criteria="Worker result is reviewed", spawn_kind="claude-code", db=db,
    )
    service.transition_node(step.id, "active", db=db)
    db.execute(
        """INSERT INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at)
           VALUES (?,'user-1','project-1','parent','native/model','t','t')""",
        (f"worker-{index}",),
    )
    worker_plan = service.ensure_plan("thread", f"worker-{index}", db=db)
    node = service.add_node(
        worker_plan.id, kind="task", title="Work", acceptance_criteria="Tests pass", db=db,
    )
    mutations.attach_spawned_thread(step.id, f"worker-{index}", db=db)
    service.transition_node(node.id, "active", actor_id=f"worker-{index}", db=db)
    submit_review_evidence(node.id, db=db, actor_id=f"worker-{index}")
    return step


def _owed_queries(db) -> int:
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        delegation_owed_verdict.owed_verdicts(db, now=NOW)
    finally:
        db.set_trace_callback(None)
    return sum(
        1 for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
    )


def test_the_sweep_reads_in_one_query_however_many_steps_are_owed_or_reminded(db):
    step, _plan, _node = _harvested(db)
    _age_return(db, step.id)
    one = _owed_queries(db)

    steps = [step] + [_more_harvested(db, index) for index in range(4)]
    delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[])
    for each in steps:
        _age_return(db, each.id)
    assert len(delegation_owed_verdict.owed_verdicts(db, now=NOW)) == 5
    many = _owed_queries(db)

    assert _sweep(db) == ["parent"]
    reminded = _owed_queries(db)

    assert delegation_owed_verdict.owed_verdicts(db, now=NOW) == []
    assert one == many == reminded == 1
