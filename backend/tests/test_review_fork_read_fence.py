"""Review fork reads use the reviewer's real registry and project fence."""

import asyncio
from types import SimpleNamespace

import pytest

from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
from core.fork_tools import execute_fork_tool
from infra import state
from test_doc_review_requests import request, review_env  # noqa: F401
from test_project_fence import world  # noqa: F401
from tools.filesystem_edit import ListDirTool
from tools.filesystem_rw import ReadFileTool
from tools.glob import GlobTool
from tools.grep import GrepTool
from tools.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["read_file", "list_dir", "glob", "grep"])
async def test_fork_and_reviewer_share_read_fence(review_env, world, monkeypatch, name):
    from tools import sandbox_config

    db, aid = review_env
    row = request(db, aid)
    db.execute("UPDATE threads SET project_id='id-alpha' WHERE id='reviewer'")
    db.commit()
    ws = world.workspace
    own = ws / "projects" / "alpha"
    other = ws / "projects" / "beta"
    (own / "allowed.txt").write_text("visible needle\n")
    (other / "secret.txt").write_text("hidden needle\n")
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD").write_text("hidden needle\n")
    monkeypatch.setattr(sandbox_config, "WORKSPACE", ws)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [ws])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    registry = ToolRegistry()
    for tool in (ReadFileTool(workspace=ws), ListDirTool(workspace=ws),
                 GlobTool(workspace=ws), GrepTool(workspace=ws)):
        registry.register(tool)
    provider = SimpleNamespace(fork_id=row["fork_id"], thread_id="reviewer", working_dir=str(ws),
                               target=SimpleNamespace(catalog_id='cloud/model'))

    def args(directory, filename):
        if name == "read_file":
            return {"path": str(directory / filename)}
        if name == "list_dir":
            return {"path": str(directory)}
        if name == "glob":
            return {"path": str(directory), "pattern": "*", "all_threads": True}
        return {"path": str(directory), "pattern": "needle", "output_mode": "content", "all_threads": True}

    async def both(arguments):
        token = state._current_thread_id.set("reviewer")
        try:
            ordinary = await registry.execute(name, arguments)
        finally:
            state._current_thread_id.reset(token)
        forked = await execute_fork_tool(ARTIFACT_REVIEW_KIND, provider, registry,
                                         asyncio.Event(), name, arguments)
        return str(ordinary), str(forked)

    normal, fork = await both(args(own, "allowed.txt"))
    assert "Error:" not in normal and "Error:" not in fork
    assert ("visible needle" if name == "read_file" else "allowed.txt") in fork
    for directory, filename in ((other, "secret.txt"), (ws / ".git", "HEAD")):
        normal, fork = await both(args(directory, filename))
        assert "hidden needle" not in normal and "hidden needle" not in fork
        assert normal.startswith("Error") and fork.startswith("Error")
        assert normal == fork

    forged = SimpleNamespace(fork_id="wrong", thread_id="reviewer", working_dir=str(ws))
    refused = await execute_fork_tool(ARTIFACT_REVIEW_KIND, forged, registry, asyncio.Event(),
                                      name, args(own, "allowed.txt"))
    assert refused == 'This review request is no longer active. ' + ARTIFACT_REVIEW_KIND.tool_refusal
