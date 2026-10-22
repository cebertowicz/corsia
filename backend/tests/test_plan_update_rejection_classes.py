"""The six #1813 plan_update rejection classes, sent through the real tool registry.

Each test sends the body an agent actually sent. Where the intent is
unambiguous the call now succeeds; otherwise the one error states the valid
shape exactly once and carries no generic retry hint, because plan tools
author their own remedy. Other tools keep the hint.
"""

from __future__ import annotations

import json

import pytest

from infra.database import get_db
from infra.db_fts import _init_messages_fts
from infra.input_admission import accept_thread_input
from plans import admission, service
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    ERROR_HINT,
    ProbeTool,
    add_task,
    execute_as,
    open_turn,
    registry_of,
)
from tools.plans import PlanEnsureTool, PlanReadTool, PlanUpdateTool


async def _update(db, thread_id: str, body: dict) -> str:
    """Send one plan_update body as the thread, inside a running Turn."""
    identity = open_turn(db, thread_id, turn_id=f"turn-{body['mutation_id']}")
    return await execute_as(
        registry_of(PlanUpdateTool()), identity, thread_id, "plan_update", body,
    )


def _body(plan_id: str, operation: str, params: dict, mutation_id: str) -> dict:
    return {
        "plan_id": plan_id, "mutation_id": mutation_id,
        "operation": operation, "params": params,
    }


def _scene(db, thread_id: str):
    plan = service.ensure_plan("thread", thread_id, db=db)
    task = add_task(db, plan.id, "Shape work")
    db.commit()
    return plan, task


@pytest.mark.asyncio
async def test_class1_start_on_a_pending_task_activates_it(isolated_db):
    thread_id = mk_session("shape-start-pending")
    db = get_db()
    plan, task = _scene(db, thread_id)

    result = await _update(db, thread_id, _body(
        plan.id, "start", {"node_id": task.id, "expected_revision": task.revision},
        "start-pending",
    ))

    assert not result.startswith("Error"), result
    assert json.loads(result)["node"]["state"] == "active"
    assert service.get_node(task.id, db=db).state == "active"
    db.close()


@pytest.mark.asyncio
async def test_class2_add_without_kind_defaults_to_task(isolated_db):
    thread_id = mk_session("shape-add-kind")
    db = get_db()
    plan, _task = _scene(db, thread_id)

    intent = "Create the new work."
    _init_messages_fts(db)
    accept_thread_input(
        thread_id, role="user", content=intent, intent="apply_now", message_id="add-no-kind-authority", db=db,
    )
    db.execute(
        "UPDATE messages SET active_turn_id='turn-add-no-kind' WHERE id='add-no-kind-authority'"
    )
    result = await _update(db, thread_id, _body(
        plan.id, "add", {
            "title": "New work", "acceptance_criteria": "Done", "size": "unknown",
            "intent": intent, "intent_words": [{"quote": intent, "role": "authority"}],
        },
        "add-no-kind",
    ))

    assert not result.startswith("Error"), result
    assert json.loads(result)["node"]["kind"] == "task"
    db.close()


@pytest.mark.asyncio
async def test_class3_unknown_operation_lists_the_operations_once(isolated_db):
    thread_id = mk_session("shape-unknown-op")
    db = get_db()
    plan, task = _scene(db, thread_id)

    result = await _update(db, thread_id, _body(
        plan.id, "complete", {"node_id": task.id, "expected_revision": 0},
        "unknown-op",
    ))

    assert result.startswith("Error"), result
    assert result.count("operation must be one of") == 1
    assert result.count("'accept_with_follow_up'") == 1
    assert ERROR_HINT not in result
    db.close()


@pytest.mark.asyncio
async def test_class4_top_level_revision_names_where_it_belongs_once(isolated_db):
    thread_id = mk_session("shape-top-revision")
    db = get_db()
    plan, task = _scene(db, thread_id)
    body = _body(plan.id, "activate", {"node_id": task.id}, "top-revision")
    body["expected_revision"] = task.revision

    result = await _update(db, thread_id, body)

    assert result.startswith("Error"), result
    assert result.count("missing required params.expected_revision") == 1
    assert ERROR_HINT not in result
    assert service.get_node(task.id, db=db).state == "pending"
    db.close()


@pytest.mark.asyncio
async def test_class5_verification_result_names_the_only_value_once(isolated_db):
    thread_id = mk_session("shape-verification")
    db = get_db()
    plan, task = _scene(db, thread_id)

    result = await _update(db, thread_id, _body(
        plan.id, "accept",
        {"node_id": task.id, "expected_revision": task.revision,
         "verification_result": "verified"},
        "bad-verification",
    ))

    assert result.startswith("Error"), result
    assert result.count("params.verification_result must be one of ['passed']") == 1
    assert ERROR_HINT not in result
    db.close()


@pytest.mark.asyncio
async def test_class6_accept_on_active_work_without_evidence_names_the_fix(isolated_db):
    """Accept settles active work in one call, so the only gap left is evidence."""
    thread_id = mk_session("shape-accept-active")
    db = get_db()
    plan, task = _scene(db, thread_id)
    active = admission.activate(
        task.id, expected_revision=task.revision, actor_id=thread_id,
        provenance="test", db=db,
    )
    db.commit()

    result = await _update(db, thread_id, _body(
        plan.id, "accept",
        {"node_id": task.id, "expected_revision": active["node"]["revision"],
         "verification_result": "passed"},
        "accept-active",
    ))

    assert result.startswith("Error: invalid request"), result
    assert result.count("use mark_done with evidence") == 1
    assert result.count("close_node is for work you are not doing") == 1
    assert ERROR_HINT not in result
    assert service.get_node(task.id, db=db).state == "active"
    db.close()


@pytest.mark.parametrize("tool", [PlanUpdateTool, PlanReadTool, PlanEnsureTool])
def test_plan_tools_author_their_own_error_remedy(tool):
    assert tool().self_describing_errors is True


@pytest.mark.asyncio
async def test_other_tools_keep_the_generic_hint(isolated_db):
    thread_id = mk_session("shape-other-tool")
    db = get_db()
    identity = open_turn(db, thread_id)
    probe = ProbeTool("hinted_probe", result="Error: probe refused")

    result = await execute_as(registry_of(probe), identity, thread_id, "hinted_probe")
    invalid = await execute_as(
        registry_of(probe), identity, thread_id, "hinted_probe", {"extra": 1},
    )

    assert probe.self_describing_errors is False
    assert result == "Error: probe refused" + ERROR_HINT
    assert invalid.endswith(ERROR_HINT)
    db.close()
