"""A workflow run ends once: a failed required step ends it, it is sized once, and it leaves one record."""

import json

import pytest

from infra.database import get_db
from llm.api_equivalent import api_equivalent
from plans import plan_wake, run_end
from plans.workflow_fields import HARD_IF_NOTICE, STEPS_CREATED
from tests.event_routing_helpers import mk_session
from plans.size_fields import TASK_SIZE_REQUIRED
from tests.test_workflow_runs import Run, SHIP_BODY

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
        review: "Closed because required task draft failed.", polish: "Closed because required task draft failed.",
    }
    assert {node for _recipient, node in told} >= {review, polish}
    assert run.rows("SELECT archived_at IS NOT NULL AS retired FROM threads WHERE id=?", worker) == [{"retired": 1}]
    assert failed["run_summary"].startswith("Run ship failed: 0 done, 0 skipped, 2 not run, 1 failed")
    (record,) = _ended(run, reply["node"]["id"])
    assert record["outcome"] == "failed"
    assert [(entry["step"], entry["state"], entry["skip_reason"]) for entry in record["steps"]] == [
        ("draft", "failed", None), ("review", "not_run", None), ("polish", "not_run", None),
    ]


async def test_a_started_step_the_failure_cuts_short_reads_stopped_with_the_reason(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, PLAIN)})
    reply = await run.start("ship")
    draft, review, polish = (step["id"] for step in reply["steps"])
    started = await run.update("start", _args(run, review))
    assert not isinstance(started, str), started

    failed = await run.update("fail", _args(run, draft))

    (record,) = _ended(run, reply["node"]["id"])
    assert [(entry["step"], entry["state"], entry["skip_reason"]) for entry in record["steps"]] == [
        ("draft", "failed", None),
        ("review", "stopped", "Closed because required task draft failed."),
        ("polish", "not_run", None),
    ]
    assert [entry["node_ids"] for entry in record["steps"]] == [[draft], [review], [polish]]
    assert failed["run_summary"].startswith("Run ship failed: 0 done, 0 skipped, 1 stopped, 1 not run, 1 failed")


async def test_a_failed_required_inner_step_fails_the_outer_run_and_an_optional_failure_ends_nothing(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, NESTED)
    reply = await run.start("outer")
    design, ship = (step["id"] for step in reply["steps"])
    sketch, note = (step["id"] for step in reply["steps"][0]["steps"])
    await run.update("fail", _args(run, note))
    assert [_state(run, node) for node in (ship, sketch, note)] == ["pending", "pending", "failed"]

    failed = await run.update("fail", _args(run, sketch))

    assert _state(run, ship) == "closed"
    assert [record["outcome"] for record in _ended(run, design)] == ["failed"]
    (record,) = _ended(run, reply["node"]["id"])
    assert (record["outcome"], {entry["step"]: entry["state"] for entry in record["steps"]}) == (
        "failed", {"design": "failed", "ship": "not_run"},
    )
    assert failed["run_summary"].startswith("Run outer failed: 0 done, 0 skipped, 1 not run, 1 failed")


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
    assert refused == f"Error: invalid request, fix and resend - {TASK_SIZE_REQUIRED}"
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

    assert last["run_summary"].startswith("Run ship completed: 2 done, 1 skipped, 0 not run, 0 failed")
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

    assert accepted["run_summary"].startswith("Run ship completed: 1 done, 0 skipped, 0 not run, 0 failed")
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


async def test_an_outer_failure_records_a_nested_run_it_cut_short_as_not_run(isolated_db, tmp_path, monkeypatch):
    outer = {
        "outer": ("# Outer\n", (
            "    - {id: brief, required: true, check: Brief exists, difficulty: {default: routine}}\n"
            "    - {id: design, workflow: inner, required: true, check: Design approved, difficulty: {default: standard}}\n"
        )),
        "inner": NESTED["inner"],
    }
    run = Run(tmp_path, monkeypatch, outer)
    reply = await run.start("outer")
    brief, design = (step["id"] for step in reply["steps"])

    failed = await run.update("fail", _args(run, brief))

    (inner,) = _ended(run, design)
    assert {entry["state"] for entry in inner["steps"]} == {"not_run"}
    (record,) = _ended(run, reply["node"]["id"])
    assert {entry["step"]: entry["state"] for entry in record["steps"]} == {"brief": "failed", "design": "not_run"}
    assert failed["run_summary"].startswith("Run outer failed: 0 done, 0 skipped, 1 not run, 1 failed")


async def test_a_failed_optional_nested_run_reads_failed_in_the_outer_record_and_ends_nothing_else(isolated_db, tmp_path, monkeypatch):
    optional = {
        "outer": ("# Outer\n", (
            "    - {id: design, workflow: inner, check: Design approved, difficulty: {default: standard}}\n"
            "    - {id: ship, required: true, check: Shipped, difficulty: {default: routine}}\n"
        )),
        "inner": NESTED["inner"],
    }
    run = Run(tmp_path, monkeypatch, optional)
    reply = await run.start("outer")
    design, ship = (step["id"] for step in reply["steps"])
    sketch, _note = (step["id"] for step in reply["steps"][0]["steps"])
    await run.update("fail", _args(run, sketch))
    assert _state(run, ship) == "pending" and _ended(run, reply["node"]["id"]) == []
    await _finish(run, ship)

    (record,) = _ended(run, reply["node"]["id"])
    assert (record["outcome"], {entry["step"]: entry["state"] for entry in record["steps"]}) == (
        "completed", {"design": "failed", "ship": "done"},
    )


MODEL = "claude-code/claude-opus-5-5"


def _seed_turn(node_id, start, end, *, thread_id, tokens=None):
    """A completed Turn of ``thread_id`` on ``node_id`` from minute ``start`` to ``end``, with a priced call of ``tokens`` input tokens."""
    from datetime import datetime, timedelta, timezone

    def at(minutes):
        return (datetime(2026, 9, 28, 12, tzinfo=timezone.utc) + timedelta(minutes=minutes)).isoformat()

    db = get_db()
    try:
        node = db.execute("SELECT plan_id,commitment_id FROM plan_nodes WHERE id=?", (node_id,)).fetchone()
        turn_id = f"turn-{node_id}-{start}"
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch,"
            "plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) "
            "VALUES (?,?,0,'test-model','completed',?,?,'boot',?,?,?,?,'execution')",
            (turn_id, thread_id, at(start), at(end), node["plan_id"], node_id, f"attempt-{turn_id}", node["commitment_id"]),
        )
        if tokens is not None:
            db.execute(
                "INSERT INTO llm_usage (id,thread_id,turn_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,"
                "cache_read_tokens,total_tokens,plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,"
                "plan_usage_role,source,created_at) "
                "VALUES (?,?,?,?,?,0,0,0,?,?,?,?,?,'execution','external_harness:claude-code','t')",
                (f"usage-{turn_id}", thread_id, turn_id, MODEL, tokens, tokens, node["plan_id"], node_id,
                 f"attempt-{turn_id}", node["commitment_id"]),
            )
        db.commit()
    finally:
        db.close()


def _settle_worker_task(task):
    from plans import closure

    db = get_db()
    try:
        closure.close_node(task, reason="Worker finished", db=db)
        db.commit()
    finally:
        db.close()


def _keep_worker_enabled(worker):
    db = get_db()
    try:
        db.execute("UPDATE threads SET agent_enabled=1 WHERE id=?", (worker,))
        db.commit()
    finally:
        db.close()


async def _delegate(run, step):
    """Attach a worker with a plan of one open task to ``step``; returns (worker, its task)."""
    from plans import service

    worker = mk_session(f"run-worker-{step}", spawned_by=run.thread_id)
    db = get_db()
    try:
        child = service.ensure_plan("thread", worker, db=db)
        task = service.add_node(child.id, kind="task", title="Work", acceptance_criteria="Worked", db=db).id
        db.commit()
    finally:
        db.close()
    attached = await run.update("attach_spawned_thread", _args(run, step, spawned_thread_id=worker))
    assert not isinstance(attached, str), attached
    return worker, task


async def test_a_run_record_carries_delegated_cost_and_counts_overlapping_steps_once(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, PLAIN)})
    reply = await run.start("ship")
    phase = reply["node"]["id"]
    draft, review, polish = (step["id"] for step in reply["steps"])
    worker, task = await _delegate(run, polish)
    _seed_turn(draft, 0, 60, thread_id=run.thread_id, tokens=1_000_000)
    _seed_turn(review, 30, 90, thread_id=run.thread_id, tokens=2_000_000)
    _seed_turn(task, 100, 110, thread_id=worker, tokens=500_000)
    _settle_worker_task(task)
    await _finish(run, draft)
    await _finish(run, review)
    await run.update("close_node", _args(run, polish, reason="Nothing to polish"))

    (record,) = _ended(run, phase)
    by_step = {entry["step"]: entry for entry in record["steps"]}
    price = lambda tokens: api_equivalent(MODEL, tokens, 0, 0, 0)  # noqa: E731
    assert by_step["polish"]["model_cost_usd"] == pytest.approx(price(500_000))
    assert (by_step["draft"]["execution_seconds"], by_step["polish"]["execution_seconds"]) == (3600, 600)
    assert (record["model_cost_usd"], record["cost_unknown_rows"]) == (pytest.approx(price(3_500_000)), 0)
    assert record["execution_seconds"] == (90 + 10) * 60  # the steps' 60 + 60 + 10 minutes, overlap counted once


async def test_archiving_an_open_optional_step_as_the_last_open_work_records_the_run(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, PLAIN)})
    reply = await run.start("ship")
    phase = reply["node"]["id"]
    draft, review, polish = (step["id"] for step in reply["steps"])
    await _finish(run, draft)
    await _finish(run, review)
    assert _ended(run, phase) == []

    archived = await run.update("archive_node", _args(run, polish))
    assert not isinstance(archived, str), archived

    (record,) = _ended(run, phase)
    by_step = {entry["step"]: entry for entry in record["steps"]}
    assert (by_step["polish"]["state"], by_step["polish"]["skip_reason"]) == ("closed", None)
    assert record["outcome"] == "completed"


async def test_a_sibling_whose_close_is_refused_stays_open_and_the_run_ends_when_it_settles(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, PLAIN)})
    reply = await run.start("ship")
    phase = reply["node"]["id"]
    draft, review, polish = (step["id"] for step in reply["steps"])
    worker, task = await _delegate(run, review)
    _keep_worker_enabled(worker)  # an enabled worker is not retired, so its open work refuses its step's close

    failed = await run.update("fail", _args(run, draft))

    assert not isinstance(failed, str), failed
    assert [_state(run, node) for node in (draft, review, polish)] == ["failed", "pending", "closed"]
    assert _ended(run, phase) == []
    assert failed["run_summary"] == run_end.RUN_LEFT_OPEN.format(titles="review")
    _settle_worker_task(task)
    await _finish(run, review)

    (record,) = _ended(run, phase)
    assert record["outcome"] == "failed"
    assert {entry["step"]: entry["state"] for entry in record["steps"]} == {
        "draft": "failed", "review": "done", "polish": "not_run",
    }


async def test_a_required_step_another_path_closes_fails_the_run(isolated_db, tmp_path, monkeypatch):
    # A retired worker's step or the person's Won't do closes a required step without failing it.
    from plans import closure
    from plans.worker_retirement import CANCELLED

    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, PLAIN)})
    reply = await run.start("ship")
    phase = reply["node"]["id"]
    draft, review, polish = (step["id"] for step in reply["steps"])
    await _finish(run, draft)
    db = get_db()
    try:
        closure.close_node(review, reason="Not wanted any more", provenance=CANCELLED, db=db)
        db.commit()
    finally:
        db.close()
    closed = await run.update("close_node", _args(run, polish, reason="Nothing to polish"))

    (record,) = _ended(run, phase)
    assert record["outcome"] == "failed"
    assert closed["run_summary"].startswith("Run ship failed: 1 done, 2 skipped")


async def test_the_persons_wont_do_on_a_required_step_fails_the_run(isolated_db, tmp_path, monkeypatch):
    from plans import user_settle

    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, PLAIN)})
    reply = await run.start("ship")
    phase = reply["node"]["id"]
    draft, review, polish = (step["id"] for step in reply["steps"])
    await _finish(run, draft)
    db = get_db()
    try:
        user_settle.settle(
            "close_node", {"node_id": review, "expected_revision": run.revision(review)}, "user",
            {"db": db, "actor_id": "user", "provenance": "user"},
        )
        db.commit()
    finally:
        db.close()

    assert _state(run, polish) == "closed"
    (record,) = _ended(run, phase)
    assert (record["outcome"], {entry["step"]: entry["state"] for entry in record["steps"]}) == (
        "failed", {"draft": "done", "review": "closed", "polish": "not_run"},
    )
