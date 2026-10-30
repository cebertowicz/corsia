"""Workflow closing roles use the existing plan review and person paths."""

import json
import uuid

import pytest

from core.skill_workflow_validation import validate_workflow
from plans import boundary, delegation, mutations, service
from plans.boundary import PlanActor
from tests.child_parent_notice_helpers import db
from tests.plan_review_helpers import submit_review_evidence
from tests.test_workflow_runs import Run, SHIP_BODY

__all__ = ["db"]


def test_by_validation():
    def errors(step):
        return validate_workflow({"workflow": {"steps": [{"id": "one", **step}]}})

    for value in ("agent", "reviewer", "person"):
        assert errors({"by": value}) == []
    assert ".by must be one of" in errors({"by": "owner"})[0]
    assert "can't be set on a step that runs a workflow" in errors({"by": "person", "workflow": "other"})[0]
    assert errors({"by": "agent", "workflow": "other"}) == []


@pytest.mark.asyncio
async def test_person_step_and_reviewer_refusals(isolated_db, tmp_path, monkeypatch):
    steps = (
        "    - {id: review, by: reviewer, required: true, check: Review passes, difficulty: {default: routine}}\n"
        "    - {id: person, by: person, title: Sign the release form, check: Form signed, instruction: '## Review', after: [review], difficulty: {default: routine}}\n"
        "    - {id: plain, difficulty: {default: routine}}\n"
    )
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, steps)})
    reply = await run.start("ship")
    reviewer = reply["steps"][0]
    assert [entry.get("by") for entry in reply["steps"]] == ["reviewer", "person", None]
    rows = run.children(reply["node"]["id"])
    assert rows[1]["assignee"] == "user"
    assert "Read it twice." in rows[1]["description"]
    assert "Check: Form signed" in rows[1]["description"]
    assert "Already tried:" not in rows[1]["description"]
    created = run.rows("SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='node_created'", reviewer["id"])
    assert json.loads(created[0]["payload_json"])["workflow_by"] == "reviewer"
    args = {"node_id": reviewer["id"], "expected_revision": run.revision(reviewer["id"])}
    assert "can't be skipped" in await run.update("close_node", {**args, "reason": "skip"})
    assert "hand it to a reviewer" in await run.update("mark_done", {**args, "evidence": "done", "verification_result": "passed"})
    await run.update("start", args)
    await run.update("request_review", {"node_id": reviewer["id"], "expected_revision": run.revision(reviewer["id"]), "evidence": "owner says done"})
    result = await run.update("accept", {"node_id": reviewer["id"], "expected_revision": run.revision(reviewer["id"]), "verification_result": "passed"})
    assert "hand it to a reviewer" in result



@pytest.mark.asyncio
async def test_person_title_fallback_and_reviewer_fail(isolated_db, tmp_path, monkeypatch):
    steps = (
        "    - {id: review, by: reviewer, difficulty: {default: routine}}\n"
        "    - {id: signature, title: Sign the release form, by: person, check: Form signed, difficulty: {default: routine}}\n"
        "    - {id: payment, title: Pay the filing fee, by: person, difficulty: {default: routine}}\n"
    )
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, steps)})
    reply = await run.start("ship")
    reviewer = reply["steps"][0]["id"]
    rows = run.children(reply["node"]["id"])
    assert rows[1]["description"] == "Sign the release form\n\nCheck: Form signed"
    assert rows[2]["description"] == "Pay the filing fee"
    await run.update("start", {"node_id": reviewer, "expected_revision": run.revision(reviewer)})
    result = await run.update("fail", {
        "node_id": reviewer, "expected_revision": run.revision(reviewer), "evidence": "Review failed",
    })
    assert result["node"]["state"] == "failed"


@pytest.mark.asyncio
async def test_person_instruction_over_limit_refuses_the_entire_run(isolated_db, tmp_path, monkeypatch):
    body = "# Ship\n\n## Long\n" + "x" * 400
    steps = "    - {id: review, by: person, instruction: '## Long', difficulty: {default: routine}}\n"
    run = Run(tmp_path, monkeypatch, {"ship": (body, steps)})
    result = await run.start("ship")
    assert "step review: a person step's instruction must fit 400 characters" in result
    assert run.node_count() == 0


def test_harvested_worker_return_can_be_accepted(db):
    from plans import workflow_fields
    parent = service.ensure_plan("thread", "parent", db=db)
    phase = service.add_node(parent.id, kind="phase", title="Run", acceptance_criteria="Done", intent="Review work", db=db)
    entry = workflow_fields.build_run(db, phase, [{"id": "review", "by": "reviewer", "difficulty": "routine"}], causing_executor="parent")[0]
    step = entry["id"]
    service.transition_node(step, "active", db=db)
    child = service.ensure_plan("thread", "worker", db=db)
    work = service.add_node(child.id, kind="task", title="Work", acceptance_criteria="Done", db=db)
    mutations.attach_spawned_thread(step, "worker", db=db)
    service.transition_node(work.id, "active", actor_id="worker", db=db)
    submit_review_evidence(work.id, db=db, actor_id="worker")
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [step]
    result = boundary.mutate(parent.id, "accept", {"node_id": step, "expected_revision": service._node_row(db, step)["revision"], "verification_result": "passed", "evidence": "Reviewed worker return"}, actor=PlanActor("parent", "user-1", "project-1"), provenance="test", mutation_id=uuid.uuid4().hex, db=db).result
    assert result["node"]["state"] == "accepted"


@pytest.mark.asyncio
async def test_person_step_after_earlier_steps_is_asked_when_they_finish(isolated_db, tmp_path, monkeypatch):
    steps = (
        "    - {id: first, by: person, title: Pick scope, difficulty: {default: routine}}\n"
        "    - {id: draft, check: Draft exists, difficulty: {default: routine}}\n"
        "    - {id: approve, by: person, title: Sign the release form, after: [draft], difficulty: {default: routine}}\n"
    )
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, steps)})
    reply = await run.start("ship")
    first, draft, approve = (entry["id"] for entry in reply["steps"])

    def asked():
        return [row["node_id"] for row in run.rows(
            "SELECT node_id FROM thread_updates WHERE kind='person_step' ORDER BY created_at",
        )]

    assert asked() == [first]
    await run.update("start", {"node_id": draft, "expected_revision": run.revision(draft)})
    await run.update("mark_done", {
        "node_id": draft, "expected_revision": run.revision(draft), "evidence": "Draft written",
        "verification_result": "passed",
    })
    assert asked() == [first, approve]
