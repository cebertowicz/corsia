"""Workspace Git tool confinement and exact commit behavior."""

from __future__ import annotations

import json
import os
import subprocess
import unicodedata
from pathlib import Path

import pytest

from tools import workspace_git
from tools.project_fence import Fence, FencedRoot


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args],
        check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Tester")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    own = tmp_path / "projects" / "own"
    other = tmp_path / "projects" / "Café"
    own.mkdir(parents=True)
    other.mkdir()
    (own / "file.txt").write_text("before\n")
    (other / "secret.txt").write_text("secret\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "initial")
    fence = Fence((FencedRoot(other, "other"),), "thread")
    monkeypatch.setattr(workspace_git, "_context", lambda thread: (tmp_path, fence, {"own", "café"}))
    monkeypatch.setattr("infra.guidance_messages.saver_author", lambda **kwargs: "Agent <agent@example.invalid>")
    return tmp_path


def test_commit_exact_files_and_history(repo):
    own = repo / "projects/own/file.txt"
    other = repo / "projects/Café/secret.txt"
    own.write_text("after\n")
    other.write_text("staged secret\n")
    git(repo, "add", "projects/Café/secret.txt")
    assert "Committed" in workspace_git._commit("thread", ["projects/own/file.txt"], "change")
    assert git(repo, "show", "--format=", "--name-only", "HEAD").strip() == "projects/own/file.txt"
    assert git(repo, "diff", "--cached", "--name-only").strip() == "\"projects/Caf\\303\\251/secret.txt\""
    assert "after" in workspace_git._history("thread", "projects/own/file.txt", 5)
    own.unlink()
    workspace_git._commit("thread", ["projects/own/file.txt"], "delete")
    assert "after" in workspace_git._history("thread", "projects/own/file.txt", 5)


@pytest.mark.parametrize("path", ["../other", "/absolute", "projects//own/file.txt", "./projects/own/file.txt", "projects/./own/file.txt", "projects/old/file.txt", "projects/CAFÉ/secret.txt", "projects/Cafe\u0301/secret.txt"])
def test_both_reject_forbidden_paths(repo, path):
    with pytest.raises(ValueError):
        workspace_git._commit("thread", [path], "message")
    with pytest.raises(ValueError):
        workspace_git._history("thread", path, 5)


def test_history_rejects_fenced_folder_and_ancestor(repo, monkeypatch):
    nested = repo / "projects/own/restricted"
    fence = Fence((FencedRoot(nested, "restricted"),), "thread")
    monkeypatch.setattr(workspace_git, "_context", lambda thread: (repo, fence, {"own"}))
    for path in ("projects/own/restricted/file", "projects/own"):
        with pytest.raises(ValueError, match="outside your scope"):
            workspace_git._history("thread", path, 5)


@pytest.mark.asyncio
async def test_project_ancestor_refuses_past_history_for_every_caller(repo, monkeypatch):
    past = repo / "projects/old"
    past.mkdir()
    (past / "secret").write_text("past-project-marker\n")
    git(repo, "add", "projects/old/secret")
    git(repo, "commit", "-qm", "past")
    git(repo, "rm", "-qr", "projects/old")
    git(repo, "commit", "-qm", "remove past")
    monkeypatch.setattr(workspace_git, "_context", lambda thread: (repo, Fence(), {"own"}))
    history = workspace_git.WorkspaceHistoryTool()
    # ASCII has no distinct NFD spelling, but it passes through the same normalization.
    for caller in ("agent", "workspace"):
        for path in ("projects", "Projects", unicodedata.normalize("NFD", "projects")):
            result = await history.execute(_thread_id=caller, path=path)
            assert result == "Error: Path is not in a current project."
            assert "past-project-marker" not in result
        assert "before" in await history.execute(_thread_id=caller, path="projects/own/file.txt")


def test_commit_rejects_unsafe_file_types(repo):
    own = repo / "projects/own"
    (own / "link").symlink_to(repo / "projects/Café/secret.txt")
    (own / "folder").symlink_to(repo / "projects/Café", target_is_directory=True)
    os.mkfifo(own / "fifo")
    os.link(own / "file.txt", own / "hardlink")
    for name in ("link", "folder/secret.txt", "fifo", ".git/config", "hardlink"):
        with pytest.raises((ValueError, OSError)):
            workspace_git._commit("thread", [f"projects/own/{name}"], "message")


def test_commit_accepts_staged_deletion_and_refuses_missing_directory(repo):
    git(repo, "rm", "projects/own/file.txt")
    with pytest.raises(ValueError, match="not a tracked file"):
        workspace_git._commit("thread", ["projects/own"], "directory")
    assert "Committed" in workspace_git._commit("thread", ["projects/own/file.txt"], "delete")
    assert git(repo, "show", "--format=", "--name-status", "HEAD").strip() == "D\tprojects/own/file.txt"


def test_commit_accepts_deleted_tracked_symlink(repo):
    link = repo / "projects/own/link"
    link.symlink_to("file.txt")
    git(repo, "add", "projects/own/link")
    git(repo, "commit", "-qm", "track link")
    link.unlink()
    assert "Committed" in workspace_git._commit("thread", ["projects/own/link"], "remove link")
    assert git(repo, "show", "--format=", "--name-status", "HEAD").strip() == "D\tprojects/own/link"


def test_ignored_and_untracked_deleted_refused(repo):
    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "projects/own/ignored.txt").write_text("ignored")
    for name in ("ignored.txt", "missing.txt"):
        with pytest.raises(ValueError):
            workspace_git._commit("thread", [f"projects/own/{name}"], "message")


def test_swapped_symlink_is_not_followed(repo, monkeypatch):
    path = repo / "projects/own/file.txt"
    original = workspace_git._read_file

    def swap(root, relative):
        path.unlink()
        path.symlink_to(repo / "projects/Café/secret.txt")
        return original(root, relative)

    monkeypatch.setattr(workspace_git, "_read_file", swap)
    with pytest.raises(OSError):
        workspace_git._commit("thread", ["projects/own/file.txt"], "message")


def test_nested_repo_does_not_redirect_or_run_hooks(repo):
    own = repo / "projects/own"
    marker = repo / "marker"
    git(own, "init", "-q")
    git(own, "config", "core.fsmonitor", f"touch {marker}")
    hook = own / ".git/hooks/pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    (own / "file.txt").write_text("nested change\n")
    workspace_git._commit("thread", ["projects/own/file.txt"], "nested change")
    assert "nested change" in workspace_git._history("thread", "projects/own/file.txt", 5)
    assert not marker.exists()


def test_workspace_agent_with_empty_fence(repo, monkeypatch):
    monkeypatch.setattr(workspace_git, "_context", lambda thread: (repo, Fence(), {"own", "café"}))
    (repo / "projects/Café/secret.txt").write_text("workspace edit\n")
    workspace_git._commit("workspace", ["projects/Café/secret.txt"], "workspace change")
    assert "workspace edit" in workspace_git._history("workspace", "projects/Café/secret.txt", 5)


def test_dot_git_file_does_not_redirect(repo):
    own = repo / "projects/own"
    (own / ".git").write_text(f"gitdir: {repo / '.git'}\n")
    (own / "file.txt").write_text("new content\n")
    workspace_git._commit("thread", ["projects/own/file.txt"], "change")
    assert "new content" in workspace_git._history("thread", "projects/own/file.txt", 5)


@pytest.mark.asyncio
async def test_native_tools_use_live_fence_and_author(tmp_path, isolated_db, monkeypatch):
    from infra import config
    from infra.database import get_db

    repo = tmp_path / "workspace"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "test@example.invalid")
    for name in ("own", "other"):
        folder = repo / "projects" / name
        folder.mkdir(parents=True)
        (folder / "file.txt").write_text(name)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    monkeypatch.setattr(config, "WORKSPACE", repo)
    with get_db() as db:
        for name in ("own", "other"):
            db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?, 't','t')", (name, name))
        db.execute("INSERT INTO threads(id,user_id,title,model,project_id,agent_enabled,created_at,updated_at) VALUES ('agent','user','Agent name','m','own',1,'t','t')")
        db.execute("INSERT INTO threads(id,user_id,title,model,agent_enabled,created_at,updated_at) VALUES ('corsia','user','CORSIA','m',1,'t','t')")
        db.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES ('corsia_agent_thread_id',?, 't')", (json.dumps({"session_id": "corsia"}),))
        db.commit()
    commit = workspace_git.WorkspaceCommitTool()
    history = workspace_git.WorkspaceHistoryTool()
    (repo / "projects/own/file.txt").write_text("changed")
    assert "Committed" in await commit.execute(_thread_id="agent", paths=["projects/own/file.txt"], message="change")
    assert git(repo, "show", "-s", "--format=%an <%ae>").strip() == "Agent name <agent@agents.corsia.local>"
    assert "changed" in await history.execute(_thread_id="agent", path="projects/own/file.txt")
    assert "outside your scope" in await history.execute(_thread_id="agent", path="projects/other/file.txt")
    (repo / "projects/other/file.txt").write_text("workspace access")
    assert "Committed" in await commit.execute(_thread_id="corsia", paths=["projects/other/file.txt"], message="workspace")
    assert "workspace access" in await history.execute(_thread_id="corsia", path="projects/other/file.txt")
