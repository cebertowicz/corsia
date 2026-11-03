"""A workflow run ends once: a failed required step ends it, it is sized once, and it leaves one record."""

import json

import pytest

from infra.database import get_db
from plans import plan_wake, run_end
from plans.workflow_fields import HARD_IF_NOTICE, STEPS_CREATED
from tests.event_routing_helpers import mk_session
from tests.test_workflow_runs import Run, SHIP_BODY
from tools.workflow_task_fields import RUN_SIZE_REQUIRED

pytestmark = pytest.mark.asyncio

PLAIN = (
    "    - {id: draft, required: true, check: Draft exists, difficulty: {default: routine}}\n"
    "    - {id: review, required: true, check: Review passes, difficulty: {default: routine}}\n"
    "    - {id: polish, check: Polished, difficulty: {default: routine}}\n"
)
NESTED = {
    "outer": ("# Outer\n", (
        "    - {id: design, workflow: inner, required: true, check: Design approved, difficulty: {default: standard}}\n"
        "    - {id: ship, required: true, check: Shipped, difficulty: {default: routine}}\n"
    )),
    "inner": ("# Inner\n", (
        "    - {id: sketch, required: true, check: Sketch exists, difficulty: {default: routine}}\n"
        "    - {id: note, check: Noted, difficulty: {default: routine}}\n"
    )),
}


def _args(run, node, **extra):
    return {"node_id": node, "expected_revision": run.revision(node), **extra}


def _state(run, node):
    return run.rows("SELECT state FROM plan_nodes WHERE id=?", node)[0]["state"]


async def _finish(run, node):
    """Mark one open step done; returns the reply."""
    return await run.update("mark_done", _args(run, node, evidence="done", verification_result="passed"))


def _ended(run, phase):
    return [json.loads(row["payload_json"]) for row in run.rows(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='workflow_run_ended'", phase,
    )]


async def test_a_failed_required_step_closes_the_rest_of_the_run_with_the_reason(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, PLAIN)})
    reply = await run.start("ship")
    draft, review, polish = (step["id"] for step in reply["steps"])
    worker = mk_session("run-worker", spawned_by=run.thread_id)
    attached = await run.update("attach_spawned_thread", _args(run, polish, spawned_thread_id=worker))
    assert not isinstance(attached, str), attached
    told = []
    monkeypatch.setattr(plan_wake, "notify_closed", lambda db, node_id, **kw: told.append((kw["executor"], node_id)))

    failed = await run.update("fail", _args(run, draft))

    states = {row["workflow_step"]: (row["state"]) for row in run.children(reply["node"]["id"])}
    assert states == {"draft": "failed", "review": "closed", "polish": "closed"}
    reasons = run.rows(
        "SELECT node_id,payload_json FROM plan_node_events WHERE event='state_transition' AND new_state='closed'",
    )
    assert {row["node_id"]: json.loads(row["payload_json"])["reason"] for row in reasons} == {
        review: "run failed: required step draft failed", polish: "run failed: required step draft failed",
    }
    assert {node for _recipient, node in told} >= {review, polish}
    assert run.rows("SELECT archived_at IS NOT NULL AS retired FROM threads WHERE id=?", worker) == [{"retired": 1}]
    assert failed["run_summary"].startswith("Run ship failed: 0 done, 2 skipped, 1 failed")
    assert [record["outcome"] for record in _ended(run, reply["node"]["id"])] == ["failed"]


async def test_a_failed_required_step_closes_nested_steps_and_an_optional_failure_ends_nothing(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, NESTED)
    reply = await run.start("outer")
    design, ship = (step["id"] for step in reply["steps"])
    sketch, note = (step["id"] for step in reply["steps"][0]["steps"])
    await run.update("fail", _args(run, note))
    assert [_state(run, node) for node in (ship, sketch, note)] == ["pending", "pending", "failed"]

    await run.update("fail", _args(run, sketch))

    assert _state(run, ship) == "pending"
    assert [record["outcome"] for record in _ended(run, design)] == ["failed"]
    assert _ended(run, reply["node"]["id"]) == []


async def test_a_sent_back_required_step_does_not_end_the_run(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, PLAIN)})
    reply = await run.start("ship")
    draft, review, polish = (step["id"] for step in reply["steps"])
    await run.update("start", _args(run, draft))
    await run.update("request_review", _args(run, draft, evidence="drafted"))
    await run.update("reject", _args(run, draft, feedback="again"))
    assert [_state(run, node) for node in (review, polish)] == ["pending", "pending"]


async def test_a_run_is_sized_once_and_hard_if_reaches_the_start_reply(isolated_db, tmp_path, monkeypatch):
    hard = (
        "    - {id: draft, check: Draft exists, difficulty: {default: standard, hard_if: 'the brief names a new vendor'}}\n"
        "    - {id: review, check: Review passes, difficulty: {default: routine}}\n"
    )
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, hard), "plain": (SHIP_BODY, PLAIN)})

    refused = await run.update("add", {
        "kind": "phase", "title": "Run ship", "acceptance_criteria": "Recorded", "workflow_ref": "ship",
    })
    assert refused == f"Error: invalid request, fix and resend - {RUN_SIZE_REQUIRED}"
    assert run.node_count() == 0

    reply = await run.start("ship")
    assert reply["steps"][0]["hard_if"] == "the brief names a new vendor"
    assert "hard_if" not in reply["steps"][1]
    assert reply["notice"] == f"{STEPS_CREATED} {HARD_IF_NOTICE}"
    assert (await run.start("plain"))["notice"] == STEPS_CREATED


async def test_the_last_close_records_the_run_once_and_the_closing_reply_says_so(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, PLAIN)})
    reply = await run.start("ship")
    phase = reply["node"]["id"]
    draft, review, polish = (step["id"] for step in reply["steps"])
    await run.update("start", _args(run, draft))
    await run.update("request_review", _args(run, draft, evidence="drafted"))
    await run.update("reject", _args(run, draft, feedback="again"))
    round_two = run.rows("SELECT id FROM plan_nodes WHERE workflow_step='draft' AND id<>?", draft)[0]["id"]

    assert "run_summary" not in await _finish(run, round_two)
    assert "run_summary" not in await run.update("close_node", _args(run, polish, reason="Nothing to polish"))
    assert _ended(run, phase) == []
    last = await _finish(run, review)

    assert last["run_summary"].startswith("Run ship completed: 2 done, 1 skipped, 0 failed")
    (record,) = _ended(run, phase)
    assert (record["workflow_ref"], record["size"], record["outcome"]) == ("ship", "M", "completed")
    by_step = {entry["step"]: entry for entry in record["steps"]}
    assert [entry["step"] for entry in record["steps"]] == ["draft", "review", "polish"]
    assert (by_step["draft"]["state"], by_step["draft"]["rounds"], by_step["draft"]["required"]) == ("done", 1, True)
    assert (by_step["polish"]["state"], by_step["polish"]["skip_reason"]) == ("closed", "Nothing to polish")
    assert by_step["review"]["skip_reason"] is None
    assert set(by_step["draft"]) >= {
        "by", "execution_seconds", "waiting_seconds", "model_cost_usd", "inner_workflow_ref", "inner_workflow_version",
    }
    db = get_db()
    try:
        run_end.record_if_ended(db, review, "accepted")
        db.commit()
    finally:
        db.close()
    assert len(_ended(run, phase)) == 1


async def test_an_accepted_last_step_records_the_run(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, "    - {id: draft, required: true, check: Draft exists, difficulty: {default: routine}}\n")})
    reply = await run.start("ship")
    draft = reply["steps"][0]["id"]
    await run.update("start", _args(run, draft))
    await run.update("request_review", _args(run, draft, evidence="drafted"))
    accepted = await run.update("accept", _args(run, draft, verification_result="passed"))

    assert accepted["run_summary"].startswith("Run ship completed: 1 done, 0 skipped, 0 failed")
    assert [record["outcome"] for record in _ended(run, reply["node"]["id"])] == ["completed"]


async def test_a_nested_run_records_itself_and_the_outer_record_reads_its_outcome(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, NESTED)
    reply = await run.start("outer")
    outer = reply["node"]["id"]
    design, ship = (step["id"] for step in reply["steps"])
    sketch, note = (step["id"] for step in reply["steps"][0]["steps"])
    assert run.rows("SELECT size FROM plan_nodes WHERE id=?", design) == [{"size": "M"}]

    await _finish(run, ship)
    await _finish(run, note)
    assert _ended(run, design) == [] and _ended(run, outer) == []
    last = await _finish(run, sketch)

    (inner,) = _ended(run, design)
    (record,) = _ended(run, outer)
    assert [(entry["step"], entry["state"]) for entry in inner["steps"]] == [("sketch", "done"), ("note", "done")]
    nested = {entry["step"]: entry for entry in record["steps"]}["design"]
    assert (nested["state"], nested["inner_workflow_ref"]) == ("done", "inner")
    assert last["run_summary"].startswith("Run outer completed: 2 done")


async def test_a_failed_nested_run_reads_failed_in_the_outer_record(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, NESTED)
    reply = await run.start("outer")
    design, ship = (step["id"] for step in reply["steps"])
    sketch, _note = (step["id"] for step in reply["steps"][0]["steps"])
    await run.update("fail", _args(run, sketch))
    assert _ended(run, reply["node"]["id"]) == []
    await _finish(run, ship)

    (record,) = _ended(run, reply["node"]["id"])
    assert (record["outcome"], {entry["step"]: entry["state"] for entry in record["steps"]}) == (
        "failed", {"design": "failed", "ship": "done"},
    )
