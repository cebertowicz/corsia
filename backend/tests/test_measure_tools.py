"""The agent-facing measure tools, run through their execute() against a temp database."""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

from automation.schedules import get as get_schedule
from automation.script_readings import build_report
from automation.script_runner import _stamped, settle
from automation.script_sandbox import ScriptRun
from core.skill_scanning import scan_scope
from core.skills_registry import SkillResolution
from infra import database
from infra.config import WORKSPACE
from infra.measure_registry import REGISTRY
from tools.automations import AutomationListTool, AutomationScheduleTool
from tests.skill_sidecar_helpers import POINTER
from tools import workflow_task_fields
from tools.measures import MeasureStatusTool, MeasureSyncTool

AGENT = "agent-tool-qg"
FIXTURE = Path(__file__).parent / "fixtures" / "measures" / "qg_measures.yaml"
GOOD = {"pr": "907", "head_sha": "abfa9122", "model": "m", "reviewer_version": "r1", "verdict": "block", "blocks": 2, "flags": 1}
NOW = "2026-01-01T12:00:00+00:00"


@pytest.fixture
def agent(isolated_db, monkeypatch):
    """A thread in a project, its own workflow skill (steps in corsia.yaml) inside the workspace, and a clean registry.

    The test project has no planning folder, so skill resolution would skip thread roots; the
    thread's skills folder is scanned for real and handed to the real owner rule instead.
    """
    REGISTRY.reset()
    with database.write_transaction(None, scope="measure_tool_seed") as db:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project-tool','Tool','t','t')")
        db.execute("INSERT INTO threads (id,user_id,project_id,created_at,updated_at) VALUES (?,?,?,?,?)", (AGENT, "fixture-user", "project-tool", NOW, NOW))
    root = WORKSPACE / ".threads" / AGENT
    skill = root / "skills" / "llm-pr-review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\nname: llm-pr-review\ndescription: Review a PR.\n{POINTER}---\n# Review\n")
    (skill / "corsia.yaml").write_text("workflow:\n  steps:\n    - {id: review, title: Review it, required: true, check: reviewed}\n")
    (skill / "measures.yaml").write_text(FIXTURE.read_text())
    monkeypatch.setattr(
        workflow_task_fields, "resolve_effective_skills",
        lambda **_kwargs: SkillResolution(scan_scope(root, "thread", AGENT), [], []),
    )
    script = root / "measure.py"
    script.write_text("print('x')")
    yield skill / "measures.yaml", script
    shutil.rmtree(root, ignore_errors=True)


def ok(result: str) -> dict:
    assert not result.startswith("Error"), result
    return json.loads(result)


@pytest.mark.asyncio
async def test_measure_sync_loads_the_file_and_automation_schedule_then_accepts_its_workflow(agent) -> None:
    measures, script = agent
    synced = ok(await MeasureSyncTool().execute(_thread_id=AGENT, path=str(measures)))
    assert synced["namespace"] == "qa"
    assert {"name": "qa.review.completed", "status": "added"} in synced["items"]
    again = ok(await MeasureSyncTool().execute(_thread_id=AGENT, path=str(measures)))
    assert {item["status"] for item in again["items"]} == {"unchanged"}
    created = ok(await AutomationScheduleTool().execute(
        _thread_id=AGENT, kind="script", script_path=str(script), cadence_seconds=300, workflow="llm-pr-review",
    ))["schedule"]
    assert created["payload"]["workflow"] == "llm-pr-review" and created["scope_id"] == "project-tool"


@pytest.mark.asyncio
async def test_automation_schedule_refuses_a_workflow_that_is_not_one_of_the_agents_skills_through_the_tool(agent) -> None:
    _, script = agent
    refused = await AutomationScheduleTool().execute(
        _thread_id=AGENT, kind="script", script_path=str(script), cadence_seconds=300, workflow="other-flow",
    )
    assert refused.startswith("Error: workflow other-flow is not one of your workflow skills (yours: llm-pr-review)")


@pytest.mark.asyncio
async def test_measure_sync_refuses_a_path_outside_the_workspace_through_the_tool(agent, tmp_path: Path) -> None:
    outside = tmp_path / "measures.yaml"
    outside.write_text(FIXTURE.read_text())
    assert await MeasureSyncTool().execute(_thread_id=AGENT, path=str(outside)) == "Error: path must be a measures.yaml file inside the workspace"


@pytest.mark.asyncio
async def test_a_relative_path_resolves_against_the_workspace_not_the_process_directory(agent, tmp_path: Path, monkeypatch) -> None:
    measures, _ = agent
    monkeypatch.chdir(tmp_path)  # a different directory with no measures.yaml of its own
    relative = measures.relative_to(WORKSPACE)
    assert ok(await MeasureSyncTool().execute(_thread_id=AGENT, path=str(relative)))["namespace"] == "qa"


@pytest.mark.asyncio
async def test_the_file_is_resolved_and_read_off_the_event_loop(agent, monkeypatch) -> None:
    measures, _ = agent
    import tools.measures as module

    loop_thread, seen = threading.get_ident(), []
    real = module._read_declaration

    def recording_read(path):
        seen.append(threading.get_ident())
        return real(path)

    monkeypatch.setattr(module, "_read_declaration", recording_read)
    ok(await MeasureSyncTool().execute(_thread_id=AGENT, path=str(measures)))
    assert seen and loop_thread not in seen


@pytest.mark.asyncio
async def test_measure_status_and_automation_list_show_a_settled_run_through_their_tools(agent, monkeypatch) -> None:
    import tools.workflow_owners as owners

    monkeypatch.setattr(owners, "_workflow_version", lambda skill: f"v-{skill.name}")  # the test workspace has no Git
    measures, script = agent
    ok(await MeasureSyncTool().execute(_thread_id=AGENT, path=str(measures)))
    schedule_id = ok(await AutomationScheduleTool().execute(
        _thread_id=AGENT, kind="script", script_path=str(script), cadence_seconds=300, workflow="llm-pr-review",
    ))["schedule"]["id"]
    sched = get_schedule(schedule_id, db=database.get_db())
    stdout = "".join(json.dumps({"measure": {"event.name": "qa.review.completed", "attributes": attributes}}) + "\n" for attributes in (GOOD, {**GOOD, "pr": "908", "verdict": "maybe"}))
    report = await _stamped(sched, build_report(sched, ScriptRun(0, stdout.encode(), b"", False), run_id="uuid-1"))
    assert report.workflow_stamp == ("llm-pr-review", "v-llm-pr-review")  # resolved through the real owner rule
    settle(sched, report, NOW, None)
    shown = ok(await MeasureStatusTool().execute(_thread_id=AGENT))
    today = shown["events"]["qa.review.completed"]["last_7_days"]
    assert today["rows"] == 2 and today["counters"] == {"attribute_dropped": 1}
    assert shown["recent_refusals"][0].startswith('qa.review.completed: attribute "verdict" value "maybe" is not one of pass, block, stop, error')
    assert [item["path"] for item in shown["synced_files"]] == [str(measures.resolve())]
    listed = ok(await AutomationListTool().execute(_thread_id=AGENT))["schedules"]
    assert listed[0]["last_measures"] == "measures: 2 recorded, 0 refused (measure_status)"
    assert listed[0]["workflow_stale"] is False and "workflow_stale" not in listed[0]["payload"]


@pytest.mark.asyncio
async def test_the_tools_need_a_conversation(isolated_db) -> None:
    assert (await MeasureSyncTool().execute(path="x")).startswith("Error: delegation tools must be called from within a conversation")
    assert (await MeasureStatusTool().execute()).startswith("Error: delegation tools must be called from within a conversation")
