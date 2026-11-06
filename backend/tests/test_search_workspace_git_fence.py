"""Searches must not expose the workspace's Git history."""

from pathlib import Path

import pytest

from tools.glob import GlobTool
from tools.grep import GrepTool
from tools.project_fence import Fence, FencedRoot

NOTICE = "Folders this agent can't read were left out."


@pytest.fixture
def git_workspace(tmp_path, monkeypatch):
    from infra import config
    from tools import project_fence, sandbox_config

    ws = tmp_path / "workspace"
    (ws / ".git").mkdir(parents=True)
    (ws / ".git" / "HEAD").write_text("secret needle\n")
    (ws / "visible.txt").write_text("needle\n")
    private = ws / "projects" / "private"
    private.mkdir(parents=True)
    monkeypatch.setattr(config, "WORKSPACE", ws)
    monkeypatch.setattr(project_fence, "fence_for_thread", lambda thread: Fence((FencedRoot(private, "private"),) if thread == "fenced" else (), thread))
    monkeypatch.setattr(sandbox_config, "WORKSPACE", ws)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [ws])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    return ws


@pytest.mark.parametrize("thread", ["unfenced", "fenced"])
@pytest.mark.parametrize("pattern", [".git/**", "**/HEAD", "**/*"])
@pytest.mark.parametrize("mode", ["glob", "content-rg", "files-rg", "count-rg", "content-python", "files-python", "count-python"])
async def test_workspace_git_never_returned(git_workspace, monkeypatch, thread, pattern, mode):
    from tools import grep

    if mode.endswith("-rg") and not grep._RG_AVAILABLE:
        pytest.skip("ripgrep unavailable")
    if mode.endswith("-python"):
        monkeypatch.setattr(grep, "_RG_AVAILABLE", False)
    if mode == "glob":
        result = await GlobTool(workspace=git_workspace).execute(pattern, _thread_id=thread)
    else:
        result = await GrepTool(workspace=git_workspace).execute("needle", glob=pattern, output_mode=mode.split("-")[0], _thread_id=thread)
    assert "secret needle" not in result
    assert ".git/HEAD" not in result
    assert NOTICE in result
    if pattern == ".git/**" and mode != "glob":
        assert "No matches" in result


async def test_git_symlink_excluded_but_nested_repository_searchable(git_workspace):
    ws = git_workspace
    (ws / "alias").symlink_to(ws / ".git", target_is_directory=True)
    nested = ws / "scratch" / ".git"
    nested.mkdir(parents=True)
    (nested / "HEAD").write_text("nested needle\n")
    result = await GlobTool(workspace=ws).execute("alias/*", _thread_id="unfenced")
    assert "alias/HEAD" not in result
    assert NOTICE in result
    result = await GlobTool(workspace=ws).execute("scratch/.git/*", _thread_id="unfenced")
    assert "scratch/.git/HEAD" in result


@pytest.mark.parametrize("mode", ["content", "files", "count"])
async def test_fallback_rechecks_directory_replaced_by_git_symlink(git_workspace, monkeypatch, mode):
    from tools import grep

    ws = git_workspace
    directory = ws / "ordinary"
    directory.mkdir()
    (directory / "HEAD").write_text("ordinary\n")
    original = grep.iter_python_files

    def swap_after_listing(*args, **kwargs):
        yield from original(*args, **kwargs)
        (directory / "HEAD").unlink()
        directory.rmdir()
        directory.symlink_to(ws / ".git", target_is_directory=True)

    monkeypatch.setattr(grep, "_RG_AVAILABLE", False)
    monkeypatch.setattr(grep, "iter_python_files", swap_after_listing)
    result = await GrepTool(workspace=ws).execute("needle", glob="ordinary/HEAD", output_mode=mode, _thread_id="unfenced")
    assert "secret needle" not in result
    assert "ordinary/HEAD" not in result


async def test_gitfile_target_symlink_is_excluded(git_workspace, tmp_path):
    ws = git_workspace
    (ws / ".git" / "HEAD").unlink()
    (ws / ".git").rmdir()
    outside = tmp_path / "gitdir"
    outside.mkdir()
    (outside / "HEAD").write_text("secret needle\n")
    (ws / ".git").write_text(f"gitdir: {outside}\n")
    result = await GlobTool(workspace=ws).execute(".git", _thread_id="unfenced")
    assert result.startswith("No files matching")
    assert NOTICE in result
    (ws / "alias").symlink_to(outside, target_is_directory=True)
    result = await GlobTool(workspace=ws).execute("alias/*", _thread_id="unfenced")
    assert "alias/HEAD" not in result
    assert NOTICE in result
