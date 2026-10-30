"""The plan-tool handoff carries the check, instruction and trial together."""
import json
from dataclasses import replace

import pytest

from infra.database import get_db
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import execute_as, open_turn, registry_of
from tests.test_lesson_delivery import _row
from tests.test_workflow_runs import Run, SHIP_BODY, SHIP_STEPS
from tools.plans import PlanReadTool


@pytest.mark.asyncio
async def test_handoff_includes_trial_with_instruction_and_check(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, SHIP_STEPS)})
    draft, review, _ = (step["id"] for step in (await run.start("ship"))["steps"])
    row = _row(tmp_path, 1, target_file="skills/ship/SKILL.md")
    lesson = replace(row.trial.lesson, case_metadata={
        "target_file": "skills/ship/SKILL.md", "target_section": "review"})
    row = replace(row, trial=replace(row.trial, lesson=lesson))
    workspace = tmp_path / "workspace"
    monkeypatch.setattr("tools.sandbox_config._STATIC_ALLOWED_RW", [workspace.resolve(), tmp_path.resolve()])
    from core.skill_scanning import scan_scope

    included = scan_scope(workspace, "workspace", "workspace")
    monkeypatch.setattr("core.skills_registry.trial_delivery_rows", lambda **_kwargs: ([row], included))
    monkeypatch.setattr("core.lesson_delivery.draw", lambda *_: "with")
    await run.update("start", {"node_id": draft, "expected_revision": run.revision(draft)})
    result = await run.update("mark_done", {
        "node_id": draft, "expected_revision": run.revision(draft), "verification_result": "passed",
        "evidence": "Draft exists", "next_node_id": review,
    })
    assert result["next"]["selected"]["acceptance_criteria"] == "Review passes"
    assert result["selected_description"]["text"] == "## Review\nRead it twice."
    assert "Check Check" in result["trial_lessons"][0]["guidance"]


@pytest.mark.asyncio
async def test_the_worker_doing_a_step_gets_its_trial_when_it_reads_the_step(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, SHIP_STEPS)})
    draft, review, _ = (step["id"] for step in (await run.start("ship"))["steps"])
    row = _row(tmp_path, 1, target_file="skills/ship/SKILL.md")
    lesson = replace(row.trial.lesson, case_metadata={
        "target_file": "skills/ship/SKILL.md", "target_section": "review"})
    row = replace(row, trial=replace(row.trial, lesson=lesson))
    workspace = tmp_path / "workspace"
    monkeypatch.setattr("tools.sandbox_config._STATIC_ALLOWED_RW", [workspace.resolve(), tmp_path.resolve()])
    from core.skill_scanning import scan_scope

    included = scan_scope(workspace, "workspace", "workspace")
    monkeypatch.setattr("core.skills_registry.trial_delivery_rows", lambda **_kwargs: ([row], included))
    monkeypatch.setattr("core.lesson_delivery.draw", lambda *_: "with")
    worker = mk_session("step-worker", spawned_by=run.thread_id)
    bystander = mk_session("step-bystander", spawned_by=run.thread_id)
    await run.update("start", {"node_id": draft, "expected_revision": run.revision(draft)})
    await run.update("mark_done", {
        "node_id": draft, "expected_revision": run.revision(draft), "verification_result": "passed",
        "evidence": "Draft exists",
    })
    attached = await run.update("attach_spawned_thread", {
        "node_id": review, "expected_revision": run.revision(review), "spawned_thread_id": worker,
    })
    assert not isinstance(attached, str), attached

    db = get_db()
    try:
        identities = {thread: open_turn(db, thread, f"turn-{thread}") for thread in (worker, bystander)}
    finally:
        db.close()

    async def read(reader, node):
        reply = await execute_as(
            registry_of(PlanReadTool()), identities[reader], reader, "plan_read",
            {"thread_id": run.thread_id, "node_id": node},
        )
        assert reply.startswith("{"), reply
        return json.loads(reply)

    assert "trial_lessons" not in await read(bystander, review)
    assert "trial_lessons" not in await read(worker, draft)
    assert run.rows("SELECT id FROM guidance_loads WHERE lesson_id IS NOT NULL") == []
    first = await read(worker, review)
    assert "Check Check" in first["trial_lessons"][0]["guidance"]
    assert (await read(worker, review))["trial_lessons"] == first["trial_lessons"]
    assert run.rows("SELECT thread_id, node_id, variant FROM guidance_loads WHERE lesson_id IS NOT NULL") == [
        {"thread_id": worker, "node_id": review, "variant": "with"},
    ]
