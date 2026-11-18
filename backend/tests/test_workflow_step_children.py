"""Workflow steps retain their smaller tasks and expose the handoff affordance."""

import json

import pytest

from tests.test_workflow_runs import Run, SHIP_BODY

pytestmark = pytest.mark.asyncio

ONE = "    - {id: draft, required: true, check: Draft exists, difficulty: {default: routine}}\n"
TWO = ONE + "    - {id: review, required: true, check: Review exists, difficulty: {default: routine}}\n"
PERSON = "    - {id: person, by: person, check: Answer exists, difficulty: {default: routine}}\n"


def args(run, node, **extra):
    return {"node_id": node, "expected_revision": run.revision(node), **extra}


async def add(run, parent=None, title="Small task", *, intent="Complete this part of the work"):
    return await run.update("add", {"kind": "task", "title": title, "acceptance_criteria": "Done",
                                    "size": "S", **({"intent": intent} if intent is not None else {}),
                                    **({"parent_id": parent} if parent else {})})


def records(run, phase):
    return [json.loads(row["payload_json"]) for row in run.rows(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='workflow_run_ended'", phase,
    )]


async def finish(run, node):
    return await run.update("mark_done", args(run, node, evidence="done", verification_result="passed"))


async def test_agent_step_accepts_child_but_person_and_plain_tasks_refuse(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, ONE), "person": (SHIP_BODY, PERSON)})
    run_reply = await run.start("ship")
    step = run_reply["steps"][0]["id"]
    child = await add(run, step)
    assert isinstance(child, dict), child
    child_id = child.get("node", child)["id"]
    row = run.rows("SELECT workflow_step,commitment_id,intent FROM plan_nodes WHERE id=?", child_id)[0]
    parent = run.rows("SELECT commitment_id FROM plan_nodes WHERE id=?", step)[0]
    assert row["workflow_step"] is None and row["commitment_id"] == parent["commitment_id"]
    assert row["intent"] == "Complete this part of the work"
    assert "task nodes are leaves" in await add(run, child_id)
    person = (await run.start("person"))["steps"][0]["id"]
    assert "The person does that step" in await add(run, person)


async def test_implicit_intent_links_to_step_canonical_intent(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, ONE)})
    reply = await run.start("ship", intent="Deliver a report")
    step = reply["steps"][0]["id"]
    child_reply = await add(run, step, intent=None)
    assert isinstance(child_reply, dict), child_reply
    child = child_reply.get("node", child_reply)["id"]
    row = run.rows("SELECT intent,intent_node_id FROM plan_nodes WHERE id=?", child)[0]
    assert row == {"intent": None, "intent_node_id": reply["node"]["id"]}
    plain = await run.update("add", {"kind": "phase", "title": "Phase", "acceptance_criteria": "Done", "size": "S"})
    phase = plain.get("node", plain)["id"]
    assert "intent" in (await add(run, phase, intent=None)).lower()


async def test_step_cannot_finish_with_open_child_and_run_waits_for_step(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, ONE)})
    reply = await run.start("ship")
    phase, step = reply["node"]["id"], reply["steps"][0]["id"]
    child_reply = await add(run, step)
    child = child_reply.get("node", child_reply)["id"]
    assert "child" in (await finish(run, step)).lower()
    assert records(run, phase) == []
    assert isinstance(await finish(run, child), dict)
    assert records(run, phase) == []
    assert isinstance(await finish(run, step), dict)
    (record,) = records(run, phase)
    assert record["outcome"] == "completed" and record["steps"][0]["state"] == "done"


async def test_failure_closes_subtask_before_its_step(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, TWO)})
    reply = await run.start("ship")
    failed, step = (entry["id"] for entry in reply["steps"])
    child_reply = await add(run, step)
    child = child_reply.get("node", child_reply)["id"]
    result = await run.update("fail", args(run, failed))
    assert isinstance(result, dict), result
    events = run.rows("SELECT node_id FROM plan_node_events WHERE event='state_transition' "
                      "AND new_state='closed' ORDER BY rowid")
    assert [row["node_id"] for row in events] == [child, step]


async def test_regroup_under_step_and_reject_plain_task(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, ONE)})
    step = (await run.start("ship"))["steps"][0]["id"]
    loose_reply = await add(run)
    loose = loose_reply.get("node", loose_reply)["id"]
    plain_reply = await add(run, title="Plain")
    plain = plain_reply.get("node", plain_reply)["id"]
    moved = await run.update("edit", args(run, loose, parent_id=step))
    assert isinstance(moved, dict), moved
    assert run.rows("SELECT parent_id FROM plan_nodes WHERE id=?", loose)[0]["parent_id"] == step
    refused = await run.update("edit", args(run, loose, parent_id=plain))
    assert "task nodes are leaves" in refused


async def test_handoff_hint_once_on_start_and_selection(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, TWO)})
    reply = await run.start("ship")
    first, second = (entry["id"] for entry in reply["steps"])
    started = await run.update("start", args(run, first))
    hint = f"You can hand this step to a worker (delegate_task with plan_node_id {first}) and add its smaller tasks under it (add with parent_id {first})."
    assert started["step_hint"] == hint
    repeated = await run.update("start", args(run, first))
    assert "step_hint" not in repeated
    done = await run.update("mark_done", args(run, first, evidence="done", verification_result="passed", next_node_id=second))
    assert done["step_hint"].count(second) == 2
