"""Reading a workflow skill says how to start its run; the skill's own text never has to."""

from __future__ import annotations

import pytest

from tests.test_workflow_runs import SHIP_BODY, SHIP_STEPS, Run

pytestmark = pytest.mark.asyncio

NOTICE = 'plan_update add with kind "phase", workflow_ref "ship"'


def _configure_sandbox(monkeypatch, workspace) -> None:
    from tools import sandbox_config

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda **_filters: [])


async def _read(run: Run, workspace, name: str, **params) -> str:
    from tools.filesystem_rw import ReadFileTool

    path = workspace / "skills" / name / "SKILL.md"
    return str(await ReadFileTool(workspace=workspace).execute(
        path=str(path), _thread_id=run.thread_id, _thread_project_id=None, **params,
    ))


async def test_a_workflow_skill_read_names_its_start_call_and_the_call_works(isolated_db, tmp_path, monkeypatch):
    from infra import config

    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, SHIP_STEPS)})
    workspace = tmp_path / "workspace"
    plain = workspace / "skills" / "plain"
    plain.mkdir()
    (plain / "SKILL.md").write_text("---\nname: plain\ndescription: Not a workflow\n---\n# Plain\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    _configure_sandbox(monkeypatch, workspace)

    text = await _read(run, workspace, "ship")
    assert text.startswith("[Workflow skill") and NOTICE in text
    assert "Write the draft." in text
    assert "[Workflow skill" not in await _read(run, workspace, "ship", offset=5)
    assert "[Workflow skill" not in await _read(run, workspace, "plain")

    reply = await run.start("ship", difficulty="standard", size="S")
    assert [step["title"] for step in reply["steps"]] == ["Draft it", "review", "polish"]


async def test_a_workflow_skill_at_a_shared_document_path_keeps_both_notes(isolated_db, tmp_path, monkeypatch):
    from infra import config
    from tools.filesystem_rw import ReadFileTool

    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, SHIP_STEPS)})
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    _configure_sandbox(monkeypatch, workspace)

    async def shared_note(*_args):
        return "[Shared document]"

    monkeypatch.setattr(ReadFileTool, "_shared_document_annotation", staticmethod(shared_note))
    text = await _read(run, workspace, "ship")
    assert text.startswith("[Shared document]\n[Workflow skill") and NOTICE in text
