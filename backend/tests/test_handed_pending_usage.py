"""A hand-off before selection still binds the worker's first call."""
from __future__ import annotations

import json

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from plans import admission, admission_attempts, service
from plans.usage_calls import binding_at_call_start, record_call_usage
from tests.external_harness_usage_helpers import persist_turn
from tests.test_delegate_task_plan_step import spawned  # noqa: F401
from tests.test_delegate_send_plan_step import sent  # noqa: F401

PARENT = "parent-1"


def _parent(db):
    db.execute("INSERT INTO threads (id,user_id,project_id,model,title,created_at,updated_at) "
               "VALUES (?,'u-test','proj-1','claude-code/opus','Parent','t','t')", (PARENT,))
    return service.ensure_plan("thread", PARENT, db=db).id


def _child(db, child):
    db.execute("INSERT INTO threads (id,user_id,project_id,spawned_by,model,title,created_at,updated_at) "
               "VALUES (?,'u-test','proj-1',?,'claude-code/sonnet','Child','t','t')", (child, PARENT))
    db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["delegate_task", "delegate_send", "plan_update"])
async def test_pending_handoff_binds_first_call(deleg_db, spawned, sent, route):
    db = deleg_db
    plan = _parent(db)
    node = service.add_node(plan, kind="task", title="Pending", acceptance_criteria="Done", db=db)
    # A different task occupies the parent's slot throughout the hand-off.
    other = service.add_node(plan, kind="task", title="Other", acceptance_criteria="Done", db=db)
    service.transition_node(other.id, "active", db=db)
    admission.start(other.id, expected_revision=service.get_node(other.id, db=db).revision,
                    actor_id=PARENT, provenance="test", db=db)
    db.commit()
    if route == "delegate_task":
        from tools.delegate_task import DelegateTaskTool
        receipt = json.loads(await DelegateTaskTool().execute(
            task="do work", model="omlx@local/qwen", _thread_id=PARENT, plan_node_id=node.id))
        child = receipt["thread_id"]
        assert receipt["plan_task"]["attached"]
    elif route == "delegate_send":
        from tools.delegate_send import DelegateSendTool
        child = "child-1"
        _child(db, child)
        receipt = json.loads(await DelegateSendTool().execute(
            child, "do work", _thread_id=PARENT, plan_node_id=node.id))
        assert receipt["plan_task"]["attached"]
    else:
        child = "child-1"
        _child(db, child)
        from tools.plans import PlanUpdateTool
        receipt = await PlanUpdateTool().execute(
            plan_id=plan, operation="attach_spawned_thread", mutation_id="attach",
            params={"node_id": node.id, "expected_revision": node.revision, "spawned_thread_id": child},
            _thread_id=PARENT,
        )
        assert not receipt.startswith("Error:"), receipt
    rows = db.execute("SELECT * FROM plan_node_attempts WHERE node_id=?", (node.id,)).fetchall()
    assert len(rows) == 1
    attempt = rows[0]
    assert attempt["executor_thread_id"] == PARENT
    assert attempt["released_at"] == attempt["selected_at"]
    assert attempt["release_reason"] == admission_attempts.HANDED_UNSELECTED
    assert service.get_node(node.id, db=db).state == "pending"
    assert admission_attempts.open_attempt(db, other.id) is not None
    from plans.operator_node_facts import attempts

    _, counts = attempts([plan], {}, limit=10, db=db)
    assert node.id not in counts
    assert admission_attempts.executor_start(db, node.id) is None
    db.commit()
    persist_turn(child, "turn-1")
    record_call_usage(child, "test-model", {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                      source="conversation", turn_id="turn-1", usage_key="first",
                      binding=binding_at_call_start(child))
    row = db.execute("SELECT plan_node_id,plan_attempt_id,plan_usage_role FROM llm_usage WHERE turn_id=?",
                     ("turn-1",)).fetchone()
    assert tuple(row) == (node.id, attempt["id"], "delegation")
    from plans import commitment_cost, feedback_join

    assert feedback_join._handed(db, node.id) is False
    cost = commitment_cost.project(node.commitment_id, db=db)
    assert cost["attempts"] == {"total": 0, "failed": 0, "correction": 0, "delegated": 0}
    assert cost["tokens"]["known"] == 12


def test_active_handoff_preserves_attempt_and_executor_start(deleg_db):
    db = deleg_db
    plan = _parent(db)
    _child(db, "child-1")
    node = service.add_node(plan, kind="task", title="Active", acceptance_criteria="Done", db=db)
    service.transition_node(node.id, "active", db=db)
    admission.start(node.id, expected_revision=service.get_node(node.id, db=db).revision,
                    actor_id=PARENT, provenance="test", db=db)
    before = admission_attempts.executor_start(db, node.id)
    attempt_id = admission_attempts.open_attempt(db, node.id)["id"]
    from plans.delegation import attach_spawned_thread
    attach_spawned_thread(node.id, "child-1", actor_id=PARENT, db=db)
    rows = db.execute("SELECT id FROM plan_node_attempts WHERE node_id=?", (node.id,)).fetchall()
    assert [row["id"] for row in rows] == [attempt_id]
    assert admission_attempts.executor_start(db, node.id) == before
    from plans.operator_node_facts import attempts
    _, counts = attempts([plan], {}, limit=10, db=db)
    assert counts[node.id] == {"attempt_count": 1, "friction_count": 0, "revision_round": 0}
    db.commit()
    persist_turn("child-1", "turn-active")
    record_call_usage("child-1", "test-model", {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                      source="conversation", turn_id="turn-active", usage_key="active",
                      binding=binding_at_call_start("child-1"))
    from tests.test_lesson_report_receipts import manifest
    from core.lesson_report_receipts import read_offers, read_offer_evidence

    manifest(db, "lesson-manifest", [{"id": "lesson", "behavioral_sha256": "a" * 64}])
    db.execute("UPDATE llm_usage SET context_manifest_id=? WHERE turn_id=?", ("lesson-manifest", "turn-active"))
    args = dict(user_id="u-test", thread_ids=("child-1",), from_ts="2000", to_ts="2100")
    assert read_offers(db, **args)["offers"][0]["attempt_id"] == attempt_id
    evidence = read_offer_evidence(db, **args)
    assert evidence["retained"][0]["attempt_id"] == attempt_id
    assert evidence["period"]["offers"].counts[("lesson", "a" * 64)]["attempts"] == 1


def test_first_real_selection_after_handoff_is_not_a_retry(deleg_db):
    from plans import delegation, operator_node_facts

    db = deleg_db
    plan = _parent(db)
    _child(db, "child-1")
    node = service.add_node(plan, kind="task", title="Pending", acceptance_criteria="Done", db=db)
    delegation.attach_spawned_thread(node.id, "child-1", actor_id=PARENT, db=db)
    delegation.detach_spawned_thread(node.id, reason="Returned", actor_id=PARENT, db=db)
    service.transition_node(node.id, "active", db=db)
    result = admission.start(node.id, expected_revision=service.get_node(node.id, db=db).revision,
                             actor_id=PARENT, provenance="test", db=db)
    selected = admission_attempts.open_attempt(db, node.id)
    assert selected["attempt_ordinal"] == 2
    assert result["admission"]["selection"]["attempt_ordinal"] == 1
    history, counts = operator_node_facts.attempts([plan], {}, limit=10, db=db)
    assert counts[node.id] == {"attempt_count": 1, "friction_count": 0, "revision_round": 0}
    assert [item["ordinal"] for item in history[node.id]] == [1]


def test_handoff_does_not_hide_the_last_real_held_task(deleg_db):
    from plans import delegation, upkeep_notice
    from plans.boundary import PlanActor
    from tests.plan_close_next_helpers import pause

    db = deleg_db
    plan = _parent(db)
    _child(db, "child-1")
    held = service.add_node(plan, kind="task", title="Held", acceptance_criteria="Done", db=db)
    service.transition_node(held.id, "active", db=db)
    admission.start(held.id, expected_revision=service.get_node(held.id, db=db).revision,
                    actor_id=PARENT, provenance="test", db=db)
    pause(db, plan, held.id, "Waiting", actor=PlanActor(PARENT, "u-test", "proj-1"))
    handed = service.add_node(plan, kind="task", title="Handed", acceptance_criteria="Done", db=db)
    service.transition_node(handed.id, "active", db=db)
    delegation.attach_spawned_thread(handed.id, "child-1", actor_id=PARENT, db=db)
    assert upkeep_notice._held_task(db, service._plan_row(db, plan), PARENT) == ("Held", "paused: Waiting")


@pytest.mark.asyncio
async def test_accepted_handed_pending_step_preserves_parent_taskless_rows():
    from registry import models
    from registry.taskless_row_backfill import retire_taskless_rows
    from tests.test_plan_pending_delegated_return import _db, _fan_out, _sweep
    from tests.test_plan_worker_retirement import _verdict
    from tests.plan_review_helpers import submit_review_evidence

    db = _db()
    try:
        note = models.register_artifact(
            integration="workspace", type="document", external_id="workspace://ws-1/.threads/parent/notes.md",
            parent_thread_id="parent", title="notes.md", retention="durable", metadata={"zone": "workspace"}, db=db,
        )
        db.execute("UPDATE artifact_registrations SET updated_at='2020-01-01T00:00:00'")
        db.commit()
        _, delegated, _, leaf = _fan_out(db)
        assert service.get_node(delegated.id, db=db).state == "pending"
        submit_review_evidence(leaf.id, db=db, evidence="Done", actor_id="worker")
        db.commit()
        await _sweep(db)
        submit_review_evidence(delegated.id, db=db, actor_id="parent")
        _verdict(db, delegated.plan_id, "accept", delegated.id)
        db.commit()
        assert service.get_node(delegated.id, db=db).state == "accepted"

        def retired():
            return db.execute("SELECT retired_at FROM artifact_registrations WHERE artifact_id=? AND parent_thread_id='parent'",
                              (note["id"],)).fetchone()[0]
        assert retired() is None
        assert "parent" not in admission_attempts.latest_accepted_starts(db)
        retire_taskless_rows(db)
        assert retired() is None
    finally:
        db.close()
