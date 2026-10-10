"""Tests for the project_repos discovery tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.query_projects import ProjectReposTool


async def _run_now(callback, *args):
    return callback(*args)


def _isolate_db_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the tool's DB access at monkeypatched, isolated surfaces."""
    monkeypatch.setattr("tools.query_projects.run_in_db", _run_now)


@pytest.mark.asyncio
async def test_project_repos_lists_registered_repositories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from infra.project_repo_policy import RepoPolicy

    _isolate_db_access(monkeypatch)
    app = tmp_path / "app"
    tools = tmp_path / "tools"
    policies = {
        "app": RepoPolicy("p1", "app", app, "dev", True),
        "tools": RepoPolicy("p1", "tools", tools, None, False),
    }
    monkeypatch.setattr(
        "infra.project_repo_policy.get_project_repo_policies",
        lambda project_id: policies,
    )

    result = await ProjectReposTool().execute(
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    lines = result.splitlines()
    assert f"- app ({app}) pinned=dev protected" in lines
    # tools is unpinned + unprotected: the exact per-repo line carries no
    # markers, proving the markers attach to app, not just appear anywhere.
    assert f"- tools ({tools})" in lines


@pytest.mark.asyncio
async def test_project_repos_requires_a_current_project() -> None:
    result = await ProjectReposTool().execute(_thread_id="thread-1")
    assert result.startswith("Error:")
    assert "current project" in result


@pytest.mark.asyncio
async def test_project_repos_reports_no_registered_repositories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_db_access(monkeypatch)
    monkeypatch.setattr(
        "infra.project_repo_policy.get_project_repo_policies",
        lambda project_id: {},
    )

    result = await ProjectReposTool().execute(
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    assert "No repositories are registered" in result
    # The next step names the agent-facing register tool, not a dead end.
    assert "project_repo_register" in result
