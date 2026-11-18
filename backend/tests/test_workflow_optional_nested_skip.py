"""Closing a workflow run's phase closes its open work: an optional nested step is skipped with one
close_node, and an abandoned run leaves no step open that no agent may close."""

from __future__ import annotations

import pytest

from tests.test_workflow_runs import Run, SHIP_BODY

pytestmark = pytest.mark.asyncio

INNER = (
    "    - {id: draft, required: true, check: Drafted, difficulty: {default: routine}}\n"
    "    - {id: deeper, workflow: leaf, check: Leafed, difficulty: {default: routine}}\n"
)
LEAF = "    - {id: note, check: Noted, difficulty: {default: routine}}\n"
OUTER = (
    "    - {id: build, required: true, check: Built, difficulty: {default: routine}}\n"
    "    - {id: review, workflow: inner, check: Reviewed, difficulty: {default: routine}}\n"
)
REASON = "No design change in this run"


def _run(tmp_path, monkeypatch, outer=OUTER):
    return Run(tmp_path, monkeypatch, {
        "outer": (SHIP_BODY, outer), "inner": (SHIP_BODY, INNER), "leaf": (SHIP_BODY, LEAF),
    })


def _subtree(run, root):
    return run.rows(
        "WITH RECURSIVE t(id) AS (SELECT ? UNION ALL SELECT n.id FROM plan_nodes n JOIN t ON n.parent_id=t.id)"
        " SELECT n.workflow_step, n.state, json_extract(e.payload_json,'$.reason') AS reason"
        " FROM plan_nodes n JOIN t ON n.id=t.id"
        " LEFT JOIN plan_node_events e ON e.node_id=n.id AND e.event='state_transition' AND e.new_state='closed'",
        root,
    )


async def _close(run, node_id):
    return await run.update("close_node", {"node_id": node_id, "expected_revision": run.revision(node_id), "reason": REASON})


async def test_closing_an_optional_nested_step_closes_its_whole_run(isolated_db, tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch)
    review = (await run.start("outer"))["steps"][1]["id"]

    result = await _close(run, review)

    assert not isinstance(result, str), result
    rows = _subtree(run, review)
    assert sorted(row["workflow_step"] for row in rows) == ["deeper", "draft", "note", "review"]
    assert {(row["state"], row["reason"]) for row in rows} == {("closed", REASON)}


async def test_the_outer_run_goes_on_after_the_skip(isolated_db, tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch)
    reply = await run.start("outer")
    build, review = (step["id"] for step in reply["steps"])

    await _close(run, review)

    assert run.rows("SELECT state FROM plan_nodes WHERE id=?", build) == [{"state": "pending"}]
    assert run.rows("SELECT state FROM plan_nodes WHERE id=?", reply["node"]["id"])[0]["state"] != "closed"


async def test_a_required_nested_step_is_still_refused_and_nothing_closes(isolated_db, tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch, OUTER.replace("workflow: inner,", "workflow: inner, required: true,"))
    review = (await run.start("outer"))["steps"][1]["id"]

    result = await _close(run, review)

    assert isinstance(result, str) and "required" in result
    assert "closed" not in {row["state"] for row in _subtree(run, review)}


async def test_a_required_step_inside_the_nested_run_is_still_refused_on_its_own(isolated_db, tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch)
    review = (await run.start("outer"))["steps"][1]["id"]
    draft = run.rows("SELECT id FROM plan_nodes WHERE parent_id=? AND workflow_step='draft'", review)[0]["id"]

    result = await _close(run, draft)

    assert isinstance(result, str) and "required" in result


async def test_a_stale_revision_closes_nothing(isolated_db, tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch)
    review = (await run.start("outer"))["steps"][1]["id"]

    result = await run.update("close_node", {"node_id": review, "expected_revision": run.revision(review) + 5, "reason": REASON})

    assert isinstance(result, str)
    assert "closed" not in {row["state"] for row in _subtree(run, review)}


async def test_closing_it_in_a_bulk_close_skips_the_run_too(isolated_db, tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch)
    review = (await run.start("outer"))["steps"][1]["id"]

    result = await run.update("close_node", {"node_ids": [review], "reason": REASON})

    assert not isinstance(result, str) and [entry["node_id"] for entry in result["closed"]] == [review], result
    assert {row["state"] for row in _subtree(run, review)} == {"closed"}


async def test_abandoning_the_run_closes_its_required_steps_too(isolated_db, tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch)
    phase = (await run.start("outer"))["node"]["id"]

    result = await _close(run, phase)

    assert not isinstance(result, str), result
    assert {(row["state"], row["reason"]) for row in _subtree(run, phase)} == {("closed", REASON)}
