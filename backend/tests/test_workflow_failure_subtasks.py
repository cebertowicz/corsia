"""Failure selection includes whole affected step subtrees."""

import pytest

from tests.test_workflow_run_end import _args, _ended, _state
from tests.test_workflow_runs import Run

pytestmark = pytest.mark.asyncio


async def test_failure_closes_dependent_subtree_and_leaves_independent_subtree(isolated_db, tmp_path, monkeypatch):
    workflows = {
        "outer": ("# Outer\n", (
            "    - {id: a, required: true, check: A, difficulty: {default: routine}}\n"
            "    - {id: b, workflow: inner, required: true, after: [a], check: B, difficulty: {default: routine}}\n"
            "    - {id: c, workflow: inner, required: true, check: C, difficulty: {default: routine}}\n"
        )),
        "inner": ("# Inner\n", (
            "    - {id: work, required: true, check: Work, difficulty: {default: routine}}\n"
        )),
    }
    run = Run(tmp_path, monkeypatch, workflows)
    reply = await run.start("outer")
    a, b, c = (step["id"] for step in reply["steps"])
    b_child = reply["steps"][1]["steps"][0]["id"]
    c_child = reply["steps"][2]["steps"][0]["id"]
    result = await run.update("fail", _args(run, a))
    assert not isinstance(result, str), result
    assert _state(run, b_child) == "closed"
    assert _state(run, c_child) == "pending"
    assert _ended(run, b)[0]["steps"][0]["state"] == "not_run"
    assert _ended(run, c) == []
