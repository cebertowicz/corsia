"""Project fences prevent searches from entering another project's files."""

from pathlib import Path

import pytest

from tools.glob import GlobTool
from tools.grep import GrepTool
from tools.project_fence import Fence, FencedRoot

NOTICE = "Other projects this agent can't read were left out."


@pytest.fixture
def fenced_workspace(tmp_path, monkeypatch):
    from tools import project_fence, sandbox_config

    ws = tmp_path / "workspace"
    public = ws / "public"
    private = ws / "projects" / "Private"
    public.mkdir(parents=True)
    private.mkdir(parents=True)
    (public / "visible.txt").write_text("needle")
    (private / "secret.txt").write_text("needle")
    monkeypatch.setattr(project_fence, "fence_for_thread", lambda _thread_id: Fence((FencedRoot(private),)))
    monkeypatch.setattr(sandbox_config, "WORKSPACE", ws)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [ws])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    return ws, private


@pytest.mark.parametrize("tool", ["glob", "grep-rg", "grep-python"])
async def test_fence_prunes_and_notifies(fenced_workspace, monkeypatch, tool):
    from tools import grep

    ws, _private = fenced_workspace
    if tool == "grep-rg" and not grep._RG_AVAILABLE:
        pytest.skip("ripgrep unavailable")
    monkeypatch.setattr(grep, "_RG_AVAILABLE", tool == "grep-rg")
    if tool == "glob":
        result = await GlobTool(workspace=ws).execute("**/*.txt", _thread_id="caller")
    else:
        result = await GrepTool(workspace=ws).execute("needle", _thread_id="caller")
    assert "visible.txt" in result
    assert "secret.txt" not in result
    assert NOTICE in result


@pytest.mark.parametrize("tool", ["glob", "grep-rg", "grep-python"])
async def test_empty_and_outside_notice(fenced_workspace, monkeypatch, tool):
    from tools import grep

    ws, _private = fenced_workspace
    if tool == "grep-rg" and not grep._RG_AVAILABLE:
        pytest.skip("ripgrep unavailable")
    monkeypatch.setattr(grep, "_RG_AVAILABLE", tool == "grep-rg")
    search = GlobTool(workspace=ws) if tool == "glob" else GrepTool(workspace=ws)
    pattern = "**/*.missing" if tool == "glob" else "not-found"
    result = await search.execute(pattern, _thread_id="caller")
    assert NOTICE in result
    result = await search.execute(pattern, path=str(ws / "public"), _thread_id="caller")
    assert NOTICE not in result


@pytest.mark.parametrize("tool", ["glob", "grep-python"])
async def test_python_walk_never_scans_fenced_directory(fenced_workspace, monkeypatch, tool):
    import os
    from tools import grep

    ws, private = fenced_workspace
    original = os.scandir
    scanned = []

    def scan(path):
        if not isinstance(path, int):
            scanned.append(Path(path))
            assert Path(path).resolve() != private.resolve()
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    monkeypatch.setattr(grep, "_RG_AVAILABLE", False)
    search = GlobTool(workspace=ws) if tool == "glob" else GrepTool(workspace=ws)
    result = await search.execute("**/*.txt" if tool == "glob" else "needle", _thread_id="caller")
    assert "visible.txt" in result
    assert scanned


@pytest.mark.parametrize("tool", ["glob", "grep"])
async def test_reviewed_outer_prunes_nested_local_only(tmp_path, monkeypatch, tool):
    import os
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tools import project_fence

    outer = tmp_path / "outer"
    nested = outer / "local"
    nested.mkdir(parents=True)
    (outer / "visible.txt").write_text("needle")
    (nested / "secret.txt").write_text("needle")
    roots = (FencedRoot(outer, "outer"), FencedRoot(nested, "local", True))
    monkeypatch.setattr(project_fence, "fence_for_thread", lambda thread_id: Fence(roots, thread_id))
    monkeypatch.setattr(project_fence, "_event", lambda *args, **kwargs: None)
    permit = AsyncMock(return_value=SimpleNamespace(resolved_path=outer))
    monkeypatch.setattr("tools.host_access_gate.consume_current_action_permit", permit)
    open_original = os.open

    def checked_open(path, flags, *args, **kwargs):
        assert str(path) != "local", "opened fenced directory"
        return open_original(path, flags, *args, **kwargs)

    search = GlobTool(workspace=tmp_path) if tool == "glob" else GrepTool(workspace=tmp_path)
    with monkeypatch.context() as guarded:
        guarded.setattr(os, "supports_dir_fd", os.supports_dir_fd | {checked_open})
        guarded.setattr(os, "open", checked_open)
        result = await search.execute("**/*.txt" if tool == "glob" else "needle", path=str(outer), _thread_id="caller")
    assert "visible.txt" in result
    assert "secret.txt" not in result
    assert NOTICE in result
    permit.assert_awaited_once()
    result = await search.execute("**/*.txt" if tool == "glob" else "needle", path=str(nested), _thread_id="caller")
    assert "Local only" in result
    assert permit.await_count == 1


@pytest.mark.parametrize("tool", ["glob", "grep-rg", "grep-python"])
async def test_unicode_case_and_symlink_fence(fenced_workspace, monkeypatch, tool):
    import unicodedata
    from tools import grep, project_fence

    ws, private = fenced_workspace
    private.rename(private.with_name("Café"))
    private = private.with_name("Café")
    alias = ws / "alias"
    alias.symlink_to(private, target_is_directory=True)
    variant = private.with_name(unicodedata.normalize("NFD", "CAFÉ"))
    monkeypatch.setattr(project_fence, "fence_for_thread", lambda thread_id: Fence((FencedRoot(variant),), thread_id))
    if tool == "grep-rg" and not grep._RG_AVAILABLE:
        pytest.skip("ripgrep unavailable")
    monkeypatch.setattr(grep, "_RG_AVAILABLE", tool == "grep-rg")
    search = GlobTool(workspace=ws) if tool == "glob" else GrepTool(workspace=ws)
    pattern = "**/*.txt" if tool == "glob" else "needle"
    result = await search.execute(pattern, _thread_id="caller")
    assert "visible.txt" in result
    assert "secret.txt" not in result
    assert NOTICE in result
    if tool == "glob":
        result = await search.execute("alias/*.txt", _thread_id="caller")
        assert "secret.txt" not in result


async def test_ripgrep_prunes_before_entering_and_keeps_gitignore(fenced_workspace):
    import subprocess
    from tools.search_fence import SearchFence
    from tools import grep

    if not grep._RG_AVAILABLE:
        pytest.skip("ripgrep unavailable")
    ws, private = fenced_workspace
    (ws / ".git").mkdir()
    (ws / ".gitignore").write_text("ignored.txt\n")
    (ws / "ignored.txt").write_text("needle")
    (private / ".gitignore").write_text("*.txt\n")
    fence = SearchFence(ws, "caller")
    result = subprocess.run(
        ["rg", "--no-config", "--debug", "--hidden", "--files", *sum((["--glob", g] for g in fence.ripgrep_globs(ws)), []), str(ws)],
        cwd=ws, capture_output=True, text=True, check=True,
    )
    assert "visible.txt" in result.stdout
    assert "ignored.txt" not in result.stdout
    assert "secret.txt" not in result.stdout
    assert f"ignoring {private}:" in result.stderr
    assert f"opened gitignore file: {private / '.gitignore'}" not in result.stderr
    output = await GrepTool(workspace=ws).execute("needle", _thread_id="caller")
    assert "visible.txt" in output
    assert "ignored.txt" not in output
    assert NOTICE in output


async def test_ripgrep_thread_root_with_nested_fence(tmp_path, monkeypatch):
    from tools import grep, project_fence, sandbox_config

    if not grep._RG_AVAILABLE:
        pytest.skip("ripgrep unavailable")
    ws = tmp_path / "workspace"
    container = ws / ".threads"
    own = container / "caller"
    hidden = own / "private"
    hidden.mkdir(parents=True)
    (own / "visible.txt").write_text("needle")
    (hidden / "secret.txt").write_text("needle")
    monkeypatch.setattr(project_fence, "fence_for_thread", lambda thread_id: Fence((FencedRoot(hidden),), thread_id))
    monkeypatch.setattr(sandbox_config, "WORKSPACE", ws)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [ws])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    output = await GrepTool(workspace=ws).execute("needle", path=str(container), _thread_id="caller")
    assert "visible.txt" in output
    assert "secret.txt" not in output
    assert NOTICE in output
