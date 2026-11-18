"""A failed workflow step affects only its dependent work."""

import json

import pytest

from plans import plan_wake
from tests.test_workflow_run_end import _args, _delegate, _ended, _finish, _settle_worker_task, _state
from tests.test_workflow_runs import Run, SHIP_BODY

pytestmark = pytest.mark.asyncio

STEPS = (
    "    - {id: a, required: true, check: A, difficulty: {default: routine}}\n"
    "    - {id: b, required: true, check: B, after: [a], difficulty: {default: routine}}\n"
    "    - {id: c, required: true, check: C, difficulty: {default: routine}}\n"
)


@pytest.mark.parametrize("delegated", [False, True])
async def test_failure_closes_dependent_and_preserves_independent(isolated_db, tmp_path, monkeypatch, delegated):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, STEPS)})
    reply = await run.start("ship")
    phase = reply["node"]["id"]
    a, b, c = (step["id"] for step in reply["steps"])
    worker = task = None
    if delegated:
        started = await run.update("start", _args(run, c))
        assert not isinstance(started, str), started
        worker, task = await _delegate(run, c)
    told = []
    monkeypatch.setattr(plan_wake, "notify_closed", lambda db, node_id, **kw: told.append(node_id))

    result = await run.update("fail", _args(run, a))
    assert not isinstance(result, str), result
    assert [_state(run, n) for n in (a, b, c)] == ["failed", "closed", "active" if delegated else "pending"]
    assert _ended(run, phase) == []
    (event,) = run.rows("SELECT payload_json,provenance FROM plan_node_events WHERE node_id=? AND new_state='closed'", b)
    assert json.loads(event["payload_json"])["reason"] == "Closed because required task a failed."
    assert event["provenance"] == "plans.workflow_run"
    assert b in told and c not in told
    if delegated:
        assert run.rows("SELECT archived_at FROM threads WHERE id=?", worker) == [{"archived_at": None}]
        assert _state(run, task) == "pending"
        _settle_worker_task(task)
    finished = await _finish(run, c)
    assert not isinstance(finished, str), finished
    (record,) = _ended(run, phase)
    assert record["outcome"] == "failed"
    assert {s["step"]: s["state"] for s in record["steps"]} == {"a": "failed", "b": "not_run", "c": "done"}
    assert finished["run_summary"].startswith("Run ship failed:")


@pytest.mark.parametrize("person_close", [False, True])
async def test_removed_dependency_is_not_resurrected_on_later_failure(isolated_db, tmp_path, monkeypatch, person_close):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, STEPS)})
    reply = await run.start("ship")
    a, b, _c = (step["id"] for step in reply["steps"])
    removed = await run.update("remove_dependency", _args(run, b, depends_on_node_id=a))
    assert not isinstance(removed, str), removed
    if person_close:
        from infra.database import get_db
        from plans import user_settle

        db = get_db()
        try:
            user_settle.settle("close_node", _args(run, a), "user", {"db": db, "actor_id": "user", "provenance": "user"})
            db.commit()
        finally:
            db.close()
    else:
        failed = await run.update("fail", _args(run, a))
        assert not isinstance(failed, str), failed
    assert _state(run, b) == "pending"


async def test_nested_failure_propagates_without_closing_inner_independent_work(isolated_db, tmp_path, monkeypatch):
    workflows = {
        "outer": ("# Outer\n", (
            "    - {id: x, workflow: inner, required: true, check: X, difficulty: {default: routine}}\n"
            "    - {id: y, required: true, check: Y, after: [x], difficulty: {default: routine}}\n"
            "    - {id: z, required: true, check: Z, difficulty: {default: routine}}\n"
        )),
        "inner": ("# Inner\n", (
            "    - {id: a, required: true, check: A, difficulty: {default: routine}}\n"
            "    - {id: c, required: true, check: C, difficulty: {default: routine}}\n"
        )),
    }
    run = Run(tmp_path, monkeypatch, workflows)
    reply = await run.start("outer")
    outer = reply["node"]["id"]
    x, y, z = (step["id"] for step in reply["steps"])
    a, c = (step["id"] for step in reply["steps"][0]["steps"])
    result = await run.update("fail", _args(run, a))
    assert not isinstance(result, str), result
    assert [_state(run, n) for n in (a, c, y, z)] == ["failed", "pending", "closed", "pending"]
    assert _ended(run, x) == [] and _ended(run, outer) == []
    await _finish(run, c)
    assert _ended(run, x)[0]["outcome"] == "failed"
    assert _ended(run, outer) == []
    await _finish(run, z)
    (record,) = _ended(run, outer)
    assert record["outcome"] == "failed"
    assert {s["step"]: s["state"] for s in record["steps"]} == {"x": "failed", "y": "not_run", "z": "done"}
