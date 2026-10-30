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


def _trial_rows(run, row):
    return run.rows(
        "SELECT thread_id, node_id, variant FROM guidance_loads WHERE lesson_id=? ORDER BY created_at, id",
        row.trial.lesson.id,
    )


def _scanned(monkeypatch, row, included=()) -> list[str]:
    """Serve ``row`` as the one trial, noting each thread that pays for the skill scan."""
    scans: list[str] = []

    def rows(*, thread_id, **_kwargs):
        scans.append(thread_id)
        return [row], list(included)

    monkeypatch.setattr("core.skills_registry.trial_delivery_rows", rows)
    return scans


def _reader(run, *threads):
    db = get_db()
    try:
        identities = {thread: open_turn(db, thread, f"turn-{thread}") for thread in threads}
    finally:
        db.close()

    async def read(reader, node):
        reply = await execute_as(
            registry_of(PlanReadTool()), identities[reader], reader, "plan_read",
            {"thread_id": run.thread_id, "node_id": node},
        )
        assert reply.startswith("{"), reply
        return json.loads(reply)

    return read


async def _attach(run, node, worker):
    attached = await run.update("attach_spawned_thread", {
        "node_id": node, "expected_revision": run.revision(node), "spawned_thread_id": worker,
    })
    assert not isinstance(attached, str), attached


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

    scans = _scanned(monkeypatch, row, scan_scope(workspace, "workspace", "workspace"))
    monkeypatch.setattr("core.lesson_delivery.draw", lambda *_: "with")
    worker = mk_session("step-worker", spawned_by=run.thread_id)
    bystander = mk_session("step-bystander", spawned_by=run.thread_id)
    await run.update("start", {"node_id": draft, "expected_revision": run.revision(draft)})
    await run.update("mark_done", {
        "node_id": draft, "expected_revision": run.revision(draft), "verification_result": "passed",
        "evidence": "Draft exists",
    })
    await _attach(run, review, worker)
    read = _reader(run, worker, bystander)

    scans.clear()
    assert "trial_lessons" not in await read(bystander, review)
    assert "trial_lessons" not in await read(worker, draft)
    assert scans == [], "a read by anyone but the step's worker never scans skills"
    assert _trial_rows(run, row) == []
    first = await read(worker, review)
    assert "Check Check" in first["trial_lessons"][0]["guidance"]
    assert (await read(worker, review))["trial_lessons"] == first["trial_lessons"]
    assert _trial_rows(run, row) == [{"thread_id": worker, "node_id": review, "variant": "with"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("settle", ["close_node", "archive_node"])
async def test_a_step_already_settled_draws_nothing_for_its_late_worker(isolated_db, tmp_path, monkeypatch, settle):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, SHIP_STEPS)})
    polish = (await run.start("ship"))["steps"][2]["id"]
    row = _row(tmp_path, 1)
    scans = _scanned(monkeypatch, row)
    monkeypatch.setattr("core.lesson_delivery.draw", lambda *_: "with")
    worker = mk_session("late-worker", spawned_by=run.thread_id)
    await _attach(run, polish, worker)
    read = _reader(run, worker)

    closed = await run.update("close_node", {
        "node_id": polish, "expected_revision": run.revision(polish), "reason": "No longer wanted",
    })
    assert not isinstance(closed, str), closed
    if settle == "archive_node":
        archived = await run.update("archive_node", {"node_id": polish, "expected_revision": run.revision(polish)})
        assert not isinstance(archived, str), archived

    assert "trial_lessons" not in await read(worker, polish)
    assert scans == []
    assert _trial_rows(run, row) == []


@pytest.mark.asyncio
async def test_a_worker_on_a_step_the_owner_started_crosses_over_on_its_next_task(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, SHIP_STEPS)})
    first = (await run.start("ship"))["steps"][0]["id"]
    second = (await run.start("ship"))["steps"][0]["id"]
    row = _row(tmp_path, 1)
    _scanned(monkeypatch, row)
    monkeypatch.setattr("core.lesson_delivery.draw", lambda _id, _sha, node: "with" if node == first else "without")
    started = await run.update("start", {"node_id": first, "expected_revision": run.revision(first)})
    assert "trial_lessons" in started
    worker = mk_session("reused-worker", spawned_by=run.thread_id)
    await _attach(run, first, worker)
    read = _reader(run, worker)

    assert "trial_lessons" in await read(worker, first)
    await run.update("fail", {"node_id": first, "expected_revision": run.revision(first)})
    await _attach(run, second, worker)
    assert "trial_lessons" not in await read(worker, second)
    assert _trial_rows(run, row) == [
        {"thread_id": run.thread_id, "node_id": first, "variant": "with"},
        {"thread_id": worker, "node_id": second, "variant": "crossover"},
    ]
