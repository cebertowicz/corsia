"""The user's Mark done and Won't do settle any open row, and everything open under it.

One test per row of ``shared/planning/plans-user-settle-any-2026-09-29.md``,
each driving the exact request the pane posts. The last test holds the menu to
the server: every action ``offered`` shows on a row is one the route accepts.
"""

from __future__ import annotations

import pytest

from plans import closure, decision_projection, service
from tests.test_attention_queue import raise_decision
from tests.test_plan_archival import _plan, _row, _task, db  # noqa: F401 - fixture
from tests.test_plan_task_steering_http import (  # noqa: F401 - fixture
    _delegated,
    _digest,
    _post,
    _row_in_pane,
    client,
)
from tests.plan_writer_authorizer import raw_plan_seed

DONE = {"mark_done": "accepted", "close_node": "closed"}


def _child(db, plan_id: str, parent_id: str, title: str, **kwargs):
    return _task(db, plan_id, title=title, parent_id=parent_id, **kwargs)


def _phase(db, plan_id: str, title: str = "Release", **kwargs):
    node = service.add_node(
        plan_id, kind="phase", title=title, acceptance_criteria=f"{title} DOD",
        db=db, **kwargs,
    )
    db.commit()
    return node


def _worker_step(db, worker_plan_id: str, title: str = "Worker step"):
    step = _task(db, worker_plan_id, title=title)
    service.transition_node(step.id, "active", actor_id="child", db=db)
    db.commit()
    return step


def _node(db, node_id: str):
    return service.get_node(node_id, db=db)


def _thread_state(db, thread_id: str) -> str:
    return db.execute(
        "SELECT lifecycle_state FROM threads WHERE id=?", (thread_id,),
    ).fetchone()["lifecycle_state"]


@pytest.mark.parametrize("action", ["mark_done", "close_node"])
def test_the_user_settles_a_task_an_agent_owns(client, db, action):
    plan = _plan(db)
    node = _task(db, plan.id, title="Agent work")

    response = _post(client, plan.id, action, node)

    assert response.status_code == 200, response.text
    assert _row(db, node.id)["state"] == DONE[action]


@pytest.mark.parametrize("action", ["mark_done", "close_node"])
def test_the_user_settles_a_parent_and_every_open_step_under_it(client, db, action):
    plan = _plan(db)
    parent = _phase(db, plan.id, "Parent")
    open_child = _phase(db, plan.id, "Open child", parent_id=parent.id)
    grandchild = _child(db, plan.id, open_child.id, "Grandchild")
    finished = _child(db, plan.id, parent.id, "Finished child")
    service.transition_node(finished.id, "active", db=db)
    closure.close_node(finished.id, reason="Not needed", db=db)
    db.commit()

    response = _post(client, plan.id, action, parent)

    assert response.status_code == 200, response.text
    for node in (parent, open_child, grandchild):
        assert _row(db, node.id)["state"] == DONE[action], node.title
    assert _row(db, finished.id)["state"] == "closed"


@pytest.mark.parametrize("action", ["mark_done", "close_node"])
def test_the_user_settles_a_track(client, db, action):
    plan = _plan(db)
    phase = service.add_node(
        plan.id, kind="track", title="Track", acceptance_criteria="Track DOD", db=db,
    )
    step = _child(db, plan.id, phase.id, "Step")

    assert _row_in_pane(db, phase.id)["user_actions"] == ["mark_done", "close_node"]
    response = _post(client, plan.id, action, phase)

    assert response.status_code == 200, response.text
    assert _row(db, phase.id)["state"] == DONE[action]
    assert _row(db, step.id)["state"] == DONE[action]


@pytest.mark.parametrize("action", ["mark_done", "close_node"])
def test_the_user_settles_delegated_work_while_its_worker_works(client, db, action):
    plan = _plan(db)
    node, worker_plan = _delegated(db, plan.id)
    step = _worker_step(db, worker_plan.id)

    response = _post(client, plan.id, action, node)

    assert response.status_code == 200, response.text
    assert _row(db, node.id)["state"] == DONE[action]
    assert _row(db, step.id)["state"] == "closed"
    assert _thread_state(db, "child") == "archived"


def test_a_settle_that_retires_a_worker_stops_its_running_turn(client, db, monkeypatch):
    from api import plans as api_plans

    stopped: list[tuple[str, str]] = []

    async def _stop(user_id: str, thread_id: str) -> None:
        stopped.append((user_id, thread_id))

    monkeypatch.setattr(api_plans, "_stop_worker_turn", _stop)
    plan = _plan(db)
    node, worker_plan = _delegated(db, plan.id)
    _worker_step(db, worker_plan.id)

    assert _post(client, plan.id, "close_node", node).status_code == 200

    assert stopped == [("user-1", "child")]


def test_a_settlement_retry_replays_without_stopping_the_worker_twice(
    client, db, monkeypatch,
):
    from api import plans as api_plans

    stopped: list[str] = []

    async def _stop(user_id: str, thread_id: str) -> None:
        stopped.append(thread_id)

    monkeypatch.setattr(api_plans, "_stop_worker_turn", _stop)
    plan = _plan(db)
    node, worker_plan = _delegated(db, plan.id)
    _worker_step(db, worker_plan.id)
    revision = _row(db, node.id)["revision"]
    body = {
        "operation": "close_node",
        "mutation_id": "settle-retry",
        "params": {"node_id": node.id, "expected_revision": revision},
    }

    first = client.post(f"/api/plans/{plan.id}/mutations", json=body)
    second = client.post(f"/api/plans/{plan.id}/mutations", json=body)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert stopped == ["child"]
    assert first.json() == second.json()


@pytest.mark.asyncio
async def test_a_stale_worker_stop_is_a_failure(monkeypatch):
    from api import plans as api_plans
    from ws import stop_control

    async def _resolve(user: dict, thread_id: str) -> str:
        return "turn-old"

    async def _stale(*args, **kwargs):
        return stop_control.StopReceipt("child", "stale_active", "turn-old")

    monkeypatch.setattr(stop_control, "resolve_active_turn_id", _resolve)
    monkeypatch.setattr(stop_control, "do_stop", _stale)

    with pytest.raises(RuntimeError, match="changed active Turns"):
        await api_plans._stop_worker_turn("user-1", "child")


def test_a_failed_worker_stop_leaves_the_task_and_worker_open(client, db, monkeypatch):
    from api import plans as api_plans

    async def _fail_stop(user_id: str, thread_id: str) -> None:
        raise RuntimeError("stop transport failed")

    monkeypatch.setattr(api_plans, "_stop_worker_turn", _fail_stop)
    plan = _plan(db)
    node, worker_plan = _delegated(db, plan.id)
    step = _worker_step(db, worker_plan.id)

    response = _post(client, plan.id, "close_node", node)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "The worker could not be stopped, so the task was not settled. Try again."
    )
    assert _row(db, node.id)["state"] == "active"
    assert _row(db, step.id)["state"] == "active"
    assert _thread_state(db, "child") == "active"


def test_settlement_refuses_if_the_worker_still_has_a_durable_running_turn(
    client, db, monkeypatch,
):
    from api import plans as api_plans

    async def _stop_without_ending_the_turn(user_id: str, thread_id: str) -> None:
        return None

    monkeypatch.setattr(api_plans, "_stop_worker_turn", _stop_without_ending_the_turn)
    plan = _plan(db)
    node, worker_plan = _delegated(db, plan.id)
    step = _worker_step(db, worker_plan.id)
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES ('turn-child','child',0,'test/model','running','t','boot')"""
    )
    db.commit()

    response = _post(client, plan.id, "close_node", node)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "The worker is still running, so the task was not settled. Try again."
    )
    assert _row(db, node.id)["state"] == "active"
    assert _row(db, step.id)["state"] == "active"
    assert _thread_state(db, "child") == "active"


def test_a_nested_delegation_under_a_parent_settles_and_stops(client, db):
    plan = _plan(db)
    parent = _phase(db, plan.id, "Parent")
    node, worker_plan = _delegated(db, plan.id)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET parent_id=? WHERE id=?", (parent.id, node.id))
    db.commit()
    step = _worker_step(db, worker_plan.id)

    response = _post(client, plan.id, "mark_done", parent)

    assert response.status_code == 200, response.text
    assert _row(db, parent.id)["state"] == "accepted"
    assert _row(db, node.id)["state"] == "accepted"
    assert _row(db, step.id)["state"] == "closed"


def test_the_user_drops_a_question_with_won_t_do(client, db):
    decision = raise_decision(db, "root", "root")
    plan_id = decision["plan_id"]
    question = _node(db, decision["node_id"])

    assert _row_in_pane(db, question.id)["user_actions"] == ["pause_node", "close_node"]
    response = _post(client, plan_id, "close_node", question)

    assert response.status_code == 200, response.text
    assert _row(db, question.id)["state"] == "closed"
    assert decision_projection.read(decision["id"], db=db)["state"] == "invalidated"


def test_marking_a_question_s_source_done_withdraws_the_question(client, db):
    decision = raise_decision(db, "root", "root")
    source = _node(db, decision["source_node_id"])

    response = _post(client, decision["plan_id"], "mark_done", source)

    assert response.status_code == 200, response.text
    assert _row(db, source.id)["state"] == "accepted"
    assert decision_projection.read(decision["id"], db=db)["state"] == "invalidated"


def test_won_t_do_on_a_parent_releases_what_waits_on_it(client, db):
    plan = _plan(db)
    parent = _phase(db, plan.id, "Parent")
    _child(db, plan.id, parent.id, "Child")
    dependent = _task(db, plan.id, title="After")
    service.add_dependency(dependent.id, parent.id, db=db)
    db.commit()

    assert _post(client, plan.id, "close_node", parent).status_code == 200

    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_dependencies WHERE node_id=?", (dependent.id,),
    ).fetchone()[0] == 0


def test_the_owner_hears_once_how_much_settled_with_the_row(client, db):
    plan = _plan(db)
    parent = _phase(db, plan.id, "Parent")
    _child(db, plan.id, parent.id, "One")
    _child(db, plan.id, parent.id, "Two")

    assert _post(client, plan.id, "close_node", parent).status_code == 200

    assert _digest(db, plan.id) == [
        'The user closed phase "Parent" and its 2 open steps (Closed by the user): '
        "do not work them.",
    ]


def _row_kinds(db):
    """One open row of every kind the matrix names, in a fresh plan each."""
    plan = _plan(db)
    parent = _phase(db, plan.id, "Parent")
    _child(db, plan.id, parent.id, "Child")
    delegated, worker_plan = _delegated(db, plan.id)
    _worker_step(db, worker_plan.id)
    held = _task(db, plan.id, title="Held", assignee="user")
    return plan.id, [parent, _phase(db, plan.id, "Empty phase"), delegated, held,
                     _task(db, plan.id, title="Agent work")]


@pytest.mark.parametrize("pick", range(5))
@pytest.mark.parametrize("slot", range(3))
def test_every_action_the_menu_offers_is_one_the_server_accepts(client, db, pick, slot):
    plan_id, rows = _row_kinds(db)
    node = rows[pick]
    offered = _row_in_pane(db, node.id)["user_actions"]

    assert {"mark_done", "close_node"} <= set(offered)
    if slot >= len(offered):
        return
    response = _post(client, plan_id, offered[slot], node)
    assert response.status_code == 200, (offered[slot], response.text)
