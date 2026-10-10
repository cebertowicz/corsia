"""Every raise site of the plan input class, reached independently.

`test_plan_mutation_error_class.py` proves the *taxonomy* across all 18
operations, but two thirds of its rows are absorbed by the shared pre-dispatch
`validate_operation_params`, which was already correct. Ablated one line at a
time, 17 of the 19 sites that commit converted could be reverted to `PlanError`
with the whole plan tier still green. Behaviour nothing pins is behaviour that
leaves.

So this file is per-site, not per-operation. Each case in
`plan_input_class_cases.py` reaches exactly one raise site through the real
mutation boundary and asserts the class, which means reverting that one line
fails exactly one case.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from plans import boundary, service
from tests.plan_input_class_cases import _CASES
from tests.plan_scene import (  # noqa: F401 - shared plan fixtures
    ACTOR,
    _DURABLE_TABLES,
    _mid,
    _revision,
    _snapshot,
    db,
    raise_decision,
    scene,
)
from tests.test_attention_queue_api import _DbProxy, _immediate


@pytest.mark.parametrize("site", sorted(_CASES), ids=lambda site: f"{site[0]}:{site[1]}")
def test_each_input_class_site_reports_a_caller_fixable_refusal(db, scene, site):
    """Reverting this one raise to `PlanError` fails here and nowhere else."""
    refuse = _CASES[site](db, scene)
    before = _snapshot(db)

    with pytest.raises(service.PlanInputError):
        refuse()

    db.rollback()
    assert _snapshot(db) == before


# --- both caller surfaces read the same class -------------------------------


def _tool(db, monkeypatch):
    """Bind `plan_update` to the fixture connection as the plan-owner thread."""
    from tools import plans as tool_plans

    monkeypatch.setattr(tool_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(tool_plans, "run_in_db", _immediate)
    monkeypatch.setattr(
        tool_plans, "_require_context",
        lambda kwargs: ("root", {"user_id": "user-1", "project_id": "project-1"}),
    )
    return tool_plans.PlanUpdateTool()


async def _over_http(db, monkeypatch, plan_id, body):
    from api import plans as api_plans

    monkeypatch.setattr(api_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(api_plans, "run_in_db", _immediate)
    return await api_plans.mutate_plan(plan_id, body, {"id": "user-1"})


def test_a_lane_owned_refusal_lives_inside_the_input_class():
    """Beside it is not good enough. A sibling class has to be re-listed at
    every surface, and the surface that forgets is the one that loops."""
    from plans.node_settlement import LaneOwnedNodeError

    assert issubclass(LaneOwnedNodeError, service.PlanInputError)


@pytest.mark.asyncio
async def test_both_surfaces_report_a_lane_owned_refusal_as_fixable(
    db, monkeypatch,
):
    """One body, both surfaces. HTTP has answered 422 since the lane rule
    shipped, but the tool matched only the input class, so a lane-owned refusal
    fell through to the conflict branch and told the agent to re-read and retry
    something re-reading cannot fix — a permanent loop on the one surface an
    agent actually uses.
    """
    decision = raise_decision(db, "root", "root")
    node_id = decision["node_id"]
    params = {"node_id": node_id, "expected_revision": _revision(db, node_id)}

    with pytest.raises(HTTPException) as refusal:
        await _over_http(db, monkeypatch, decision["plan_id"], {
            "operation": "fail", "mutation_id": _mid(), "params": params,
        })
    db.rollback()
    reported = await _tool(db, monkeypatch).execute(
        plan_id=decision["plan_id"], mutation_id=_mid(), operation="fail",
        params=params,
    )

    assert refusal.value.status_code == 422
    assert "invalid request" in reported.casefold()
    assert "conflict" not in reported.casefold()
    assert "re-read" not in reported.casefold()
    # Both name the operation that does own the outcome, so the fix is reachable.
    assert "invalidate" in str(refusal.value.detail)
    assert "invalidate" in reported
@pytest.mark.asyncio
async def test_tool_arms_and_resolves_a_concrete_object_condition(db, scene, monkeypatch):
    """The declared tool contract must admit the runtime validator's object."""
    tool = _tool(db, monkeypatch)
    arm_params = {
        "node_id": scene.own,
        "expected_revision": _revision(db, scene.own),
        "condition": {
            "type": "deadline",
            "value": "2030-01-01T00:00:00+00:00",
        },
        "expected_event": "external.done",
    }
    request = {
        "plan_id": scene.plan_id,
        "mutation_id": _mid(),
        "operation": "arm_watch",
        "params": arm_params,
    }

    assert tool.validate_params(request) == []
    armed = await tool.execute(**request)
    watch_id = json.loads(armed)["watch"]["id"]

    resolved = await tool.execute(
        plan_id=scene.plan_id,
        mutation_id=_mid(),
        operation="resolve_watch",
        params={
            "node_id": scene.own,
            "expected_revision": _revision(db, scene.own),
            "watch_id": watch_id,
        },
    )

    assert json.loads(resolved)["watch"]["status"] == "resolved"


@pytest.mark.asyncio
async def test_tool_refuses_a_malformed_object_condition_as_caller_fixable(
    db, scene, monkeypatch,
):
    """A schema-valid malformed deadline is a correction, never a 500."""
    tool = _tool(db, monkeypatch)
    reported = await tool.execute(
        plan_id=scene.plan_id,
        mutation_id=_mid(),
        operation="arm_watch",
        params={
            "node_id": scene.own,
            "expected_revision": _revision(db, scene.own),
            "condition": {"type": "deadline", "value": "not-a-timestamp"},
            "expected_event": "external.done",
        },
    )

    assert "invalid request, fix and resend" in reported
    assert "not a valid ISO-8601 timestamp" in reported
