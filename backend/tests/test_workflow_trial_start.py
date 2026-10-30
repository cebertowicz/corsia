"""The plan-tool handoff carries the check, instruction and trial together."""
from dataclasses import replace

import pytest

from tests.test_lesson_delivery import _row
from tests.test_workflow_runs import Run, SHIP_BODY, SHIP_STEPS


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
    monkeypatch.setattr("core.skills_registry.trial_delivery_rows", lambda **kwargs: ([row], [])
                        if kwargs["workflow_step"] == "review" else ([], []))
    monkeypatch.setattr("core.lesson_delivery.draw", lambda *_: "with")
    await run.update("start", {"node_id": draft, "expected_revision": run.revision(draft)})
    result = await run.update("mark_done", {
        "node_id": draft, "expected_revision": run.revision(draft), "verification_result": "passed",
        "evidence": "Draft exists", "next_node_id": review,
    })
    assert result["next"]["selected"]["acceptance_criteria"] == "Review passes"
    assert result["selected_description"]["text"] == "## Review\nRead it twice."
    assert "Check Check" in result["trial_lessons"][0]["guidance"]
