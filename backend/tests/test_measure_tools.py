"""The agent-facing measure tools, run through their execute() against a temp database."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from automation.schedules import get as get_schedule
from automation.script_readings import build_report
from automation.script_runner import settle
from automation.script_sandbox import ScriptRun
from infra import database
from infra.config import WORKSPACE
from infra.measure_registry import REGISTRY
from tools.automations import AutomationListTool, AutomationScheduleTool
from tools.measures import MeasureStatusTool, MeasureSyncTool

AGENT = "agent-tool-qg"
FIXTURE = Path(__file__).parent / "fixtures" / "measures" / "qg_measures.yaml"
GOOD = {"pr": "907", "head_sha": "abfa9122", "model": "m", "reviewer_version": "r1", "verdict": "block", "blocks": 2, "flags": 1}
NOW = "2026-01-01T12:00:00+00:00"


@pytest.fixture
def agent(isolated_db):
    """A thread in a project, its own workflow skill folder inside the workspace, and a clean registry."""
    REGISTRY.events, REGISTRY.workflows, REGISTRY.namespaces, REGISTRY.loaded = {}, {}, {}, False
    REGISTRY.refusals.clear()
    with database.write_transaction(None, scope="measure_tool_seed") as db:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project-tool','Tool','t','t')")
        db.execute("INSERT INTO threads (id,user_id,project_id,created_at,updated_at) VALUES (?,?,?,?,?)", (AGENT, "fixture-user", "project-tool", NOW, NOW))
    root = WORKSPACE / ".threads" / AGENT
    skill = root / "skills" / "llm-pr-review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\nname: llm-pr-review\ncorsia:\n  workflow:\n    owner: {AGENT}\n---\n")
    (skill / "measures.yaml").write_text(FIXTURE.read_text())
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
async def test_automation_schedule_refuses_a_workflow_no_sync_declared_through_the_tool(agent) -> None:
    _, script = agent
    refused = await AutomationScheduleTool().execute(
        _thread_id=AGENT, kind="script", script_path=str(script), cadence_seconds=300, workflow="llm-pr-review",
    )
    assert refused.startswith('Error: workflow "llm-pr-review" is not declared by any synced measures.yaml')


@pytest.mark.asyncio
async def test_measure_sync_refuses_a_path_outside_the_workspace_through_the_tool(agent, tmp_path: Path) -> None:
    outside = tmp_path / "measures.yaml"
    outside.write_text(FIXTURE.read_text())
    assert await MeasureSyncTool().execute(_thread_id=AGENT, path=str(outside)) == "Error: path must be a measures.yaml file inside the workspace"


@pytest.mark.asyncio
async def test_measure_status_and_automation_list_show_a_settled_run_through_their_tools(agent) -> None:
    measures, script = agent
    ok(await MeasureSyncTool().execute(_thread_id=AGENT, path=str(measures)))
    schedule_id = ok(await AutomationScheduleTool().execute(
        _thread_id=AGENT, kind="script", script_path=str(script), cadence_seconds=300, workflow="llm-pr-review",
    ))["schedule"]["id"]
    sched = get_schedule(schedule_id, db=database.get_db())
    stdout = "".join(json.dumps({"measure": {"event.name": "qa.review.completed", "attributes": attributes}}) + "\n" for attributes in (GOOD, {**GOOD, "pr": "908", "verdict": "maybe"}))
    settle(sched, build_report(sched, ScriptRun(0, stdout.encode(), b"", False), run_id="uuid-1"), NOW, None)
    shown = ok(await MeasureStatusTool().execute(_thread_id=AGENT))
    today = shown["events"]["qa.review.completed"]["last_7_days"]
    assert today["rows"] == 2 and today["counters"] == {"attribute_dropped": 1}
    assert shown["recent_refusals"][0].startswith('qa.review.completed: attribute "verdict" value "maybe" is not one of pass, block, stop, error')
    assert [item["path"] for item in shown["synced_files"]] == [str(measures.resolve())]
    listed = ok(await AutomationListTool().execute(_thread_id=AGENT))["schedules"]
    assert listed[0]["payload"]["last_measures"] == "measures: 2 recorded, 0 refused (measure_status)"
    assert listed[0]["payload"]["workflow_stale"] is False


@pytest.mark.asyncio
async def test_the_tools_need_a_conversation(isolated_db) -> None:
    assert (await MeasureSyncTool().execute(path="x")).startswith("Error: delegation tools must be called from within a conversation")
    assert (await MeasureStatusTool().execute()).startswith("Error: delegation tools must be called from within a conversation")
