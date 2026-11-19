"""Workflow step children yield back to their step after they settle."""

import pytest

from infra.database import get_db
from plans import (
    aggregates_rebuild, close_next, handoff_brief, node_states, projections, run_end,
    selection_validity, service,
)
from plans.aggregates_recount import stored_mismatches
from tests.test_workflow_runs import Run, SHIP_BODY
from tests.test_workflow_step_children import ONE, add, args, finish, records

pytestmark = pytest.mark.asyncio
REVIEW = "    - {id: review, by: reviewer, check: Reviewed, difficulty: {default: routine}}\n"


async def test_finished_child_hands_step_back_as_work(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, ONE)})
    reply = await run.start("ship")
    phase, step = reply["node"]["id"], reply["steps"][0]["id"]
    created = await add(run, step)
    child = created.get("node", created)["id"]
    other_reply = await add(run, step, title="Another subtask")
    other = other_reply.get("node", other_reply)["id"]
    assert isinstance(await finish(run, other), dict)
    db = get_db()
    try:
        assert child in run_end.open_nodes(db, phase)
        assert step not in run_end.open_nodes(db, phase)
        assert step in node_states.parent_ids(service._plan_tree_rows(db, run.plan_id))
        candidates = [(service._node_row(db, step), service._plan_row(db, run.plan_id))]
        assert selection_validity._blockers(db, candidates)[step] == selection_validity.HAS_CHILDREN
        assert not stored_mismatches(db, run.plan_id)
    finally:
        db.close()
    finished = await finish(run, child)
    assert isinstance(finished, dict), finished
    db = get_db()
    try:
        rows = [dict(row) for row in service._plan_tree_rows(db, run.plan_id)]
        assert step in {row["id"] for row in node_states.leaves(rows)}
        assert step in projections.liveness_signal(run.thread_id, db=db).node_ids
        assert projections.movable_work(db, run.plan_id)
        next_work = close_next.executor_next(db, run.plan_id, run.thread_id)
        assert step in {row["id"] for row in next_work["open_tasks"]}
        assert step not in node_states.folded_ids(rows)
        assert step in run_end.open_nodes(db, phase)
        assert not stored_mismatches(db, run.plan_id)
        aggregates_rebuild.rebuild(db, run.plan_id)
        assert not stored_mismatches(db, run.plan_id)
    finally:
        db.close()
    selected = await run.update("start", args(run, step))
    assert selected["admission"]["selection"] is not None, selected
    assert selected["node"]["id"] == step
    assert records(run, phase) == []
    assert isinstance(await finish(run, step), dict)
    assert records(run, phase)[0]["outcome"] == "completed"


async def test_moving_an_open_child_updates_the_step_and_rebuild(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, ONE)})
    step = (await run.start("ship"))["steps"][0]["id"]
    child_reply = await add(run, step)
    child = child_reply.get("node", child_reply)["id"]
    moved = await run.update("edit", args(run, child, parent_id=None))
    assert isinstance(moved, dict), moved
    db = get_db()
    try:
        assert step not in node_states.parent_ids(service._plan_tree_rows(db, run.plan_id))
        assert not stored_mismatches(db, run.plan_id)
        aggregates_rebuild.rebuild(db, run.plan_id)
        assert not stored_mismatches(db, run.plan_id)
    finally:
        db.close()


async def test_plain_parent_with_accepted_child_stays_a_container(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, ONE)})
    phase_reply = await run.update("add", {"kind": "phase", "title": "Plain", "acceptance_criteria": "Done", "size": "S", "intent": "Work"})
    phase = phase_reply["node"]["id"]
    created = await add(run, phase)
    child = created.get("node", created)["id"]
    assert isinstance(await finish(run, child), dict)
    db = get_db()
    try:
        rows = [dict(row) for row in service._plan_tree_rows(db, run.plan_id)]
        assert phase in node_states.parent_ids(rows)
        assert phase in node_states.folded_ids(rows)
    finally:
        db.close()


async def test_reviewer_step_refuses_children_and_has_no_hint(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"review": (SHIP_BODY, REVIEW)})
    step = (await run.start("review"))["steps"][0]["id"]
    refused = await add(run, step)
    assert isinstance(refused, str) and "review" in refused.lower()
    db = get_db()
    try:
        assert "step_hint" not in handoff_brief.fact(db, step, run.thread_id)
    finally:
        db.close()
