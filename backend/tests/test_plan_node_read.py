"""Reading one node as the pane lists a row: the details a folded-history row opens on (#149)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plans import closure, node_states, open_tree, operator_projection, service
from plans.operator_node_read import read_node
from plans.operator_projection import OperatorNotFound
from tests.plan_scale_fixture import build
from tests.test_plan_node_cost import _usage
from tests.test_plan_open_tree_scale import ACTOR, HUMAN, SIZES, STEP_TOLERANCE, measure
from tests.test_plan_operator_api import _accept, _actor, _DbProxy, _immediate, _task, db  # noqa: F401 - fixture


def _history(db):
    """A plan with accepted work carrying evidence and cost, a closed row, an open task and a folded branch."""
    plan = service.ensure_plan("thread", "parent", db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Delivery", acceptance_criteria="Delivery DOD", db=db,
    )
    done = _task(plan.id, "Shipped", parent_id=phase.id, db=db)
    _accept(done.id, db=db)
    _usage(db, done, tokens=120, cost=0.25, usage_id="u-done")
    closed = _task(plan.id, "Dropped", parent_id=phase.id, db=db)
    closure.close_node(closed.id, reason="superseded by the shipped work", db=db)
    live = _task(plan.id, "Still open", parent_id=phase.id, db=db)
    old = service.add_node(
        plan.id, kind="phase", title="Earlier", acceptance_criteria="Earlier DOD", db=db,
    )
    older = _task(plan.id, "Earlier work", parent_id=old.id, db=db)
    _accept(older.id, db=db)
    closure.close_node(old.id, reason="finished earlier", db=db)
    return plan, phase, done, closed, live, old, older


def _listed(db, plan_id: str, node_id: str) -> dict:
    """The node as the pane lists it, with every folded row in the window."""
    projected = operator_projection.open_rollup.rollup_thread(
        plan_id, db=db, window=open_tree.FoldWindow(since=None, cap=50),
    )
    plans = operator_projection.presented_plans([projected], _actor(), db=db, include_archived=True)
    return next(node for node in plans[0]["nodes"] if node["id"] == node_id)


def test_a_folded_row_reads_as_the_pane_lists_it(db):
    plan, phase, done, closed, live, old, older = _history(db)
    for node in (done, closed):
        assert read_node(plan.id, node.id, actor=_actor(), db=db) == _listed(db, plan.id, node.id)
    shipped = read_node(plan.id, done.id, actor=_actor(), db=db)
    assert shipped["evidence"] and shipped["node_cost"]["own"]
    assert shipped["node_cost"]["own"]["tokens"]["known"] == 120
    dropped = read_node(plan.id, closed.id, actor=_actor(), db=db)
    assert dropped["status_reason"] == "superseded by the shipped work"
    assert dropped["state"] in node_states._TERMINAL_STATES
    # A row below a folded branch is outside the pane's window; its details still read.
    assert read_node(plan.id, older.id, actor=_actor(), db=db)["state"] == "accepted"


def test_a_folded_branch_reads_as_the_pane_lists_it(db):
    plan, phase, done, closed, live, old, older = _history(db)
    assert read_node(plan.id, old.id, actor=_actor(), db=db) == _listed(db, plan.id, old.id)


def test_refusals_read_alike(db):
    plan, phase, done, closed, live, old, older = _history(db)
    foreign = service.ensure_plan("thread", "foreign", db=db)
    foreign_node = _task(foreign.id, "Foreign work", db=db)
    for call in (
        lambda: read_node("missing", done.id, actor=_actor(), db=db),
        lambda: read_node(plan.id, "missing", actor=_actor(), db=db),
        lambda: read_node(plan.id, foreign_node.id, actor=_actor(), db=db),
        lambda: read_node(foreign.id, foreign_node.id, actor=_actor(), db=db),
        lambda: read_node(plan.id, done.id, actor=_actor("user-2"), db=db),
        lambda: read_node(plan.id, done.id, actor=ACTOR, db=db),
    ):
        with pytest.raises(OperatorNotFound):
            call()


@pytest.mark.asyncio
async def test_http_route_serves_the_owner_and_hides_other_users_plans(db, monkeypatch):
    from api import plans_folded as api_folded
    plan, phase, done, closed, live, old, older = _history(db)
    proxy = _DbProxy(db)
    monkeypatch.setattr(api_folded, "get_db", lambda: proxy)
    monkeypatch.setattr(api_folded, "run_read", _immediate)
    app = FastAPI()
    app.include_router(api_folded.router)
    app.dependency_overrides[api_folded.require_user] = lambda: {"id": "user-1"}
    with TestClient(app) as client:
        served = client.get(f"/api/plans/{plan.id}/nodes/{done.id}")
        assert served.status_code == 200
        assert served.json()["id"] == done.id and served.json()["evidence"]
        missing = client.get(f"/api/plans/{plan.id}/nodes/missing")
        assert missing.status_code == 404
    app.dependency_overrides[api_folded.require_user] = lambda: {"id": "user-2"}
    with TestClient(app) as client:
        refused = client.get(f"/api/plans/{plan.id}/nodes/{done.id}")
        assert refused.status_code == 404 and refused.json() == missing.json()


def _folded_leaf(scale) -> str:
    """The newest folded settled leaf: a row the pane lists in its window, whatever the history."""
    return scale.db.execute(
        "SELECT id FROM plan_nodes WHERE plan_id=? AND folded=1 AND state='accepted' AND sent_back=0"
        " ORDER BY own_last_changed DESC, id LIMIT 1", (scale.plan_id,),
    ).fetchone()["id"]


def test_the_node_read_costs_the_node_not_the_plan_history():
    steps: dict[int, int] = {}
    for size in SIZES:
        scale = build(size)
        node_id = _folded_leaf(scale)

        def call():
            return read_node(scale.plan_id, node_id, actor=HUMAN, db=scale.db)

        call()  # primes statement caches so only steady-state work is counted
        steps[size], row = measure(scale.db, call)
        assert row["id"] == node_id and row["state"] == "accepted"
        scale.db.close()
    print(f"\nnode read steps by settled-history size: {steps}")
    small, large = min(steps), max(steps)
    assert steps[large] <= steps[small] * STEP_TOLERANCE + 100, steps
