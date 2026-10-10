from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.project_context import resolve_project_paths
from infra.platform_paths import WorkspaceSelection


@pytest.fixture
def project_db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT);
        CREATE TABLE project_repo_policies (
            project_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            pinned_branch TEXT,
            protect_base_checkout INTEGER NOT NULL,
            PRIMARY KEY (project_id, repo_name)
        );
        """
    )
    yield connection
    connection.close()


def _selection(root: Path, workspace_id: str = "workspace-123") -> WorkspaceSelection:
    return WorkspaceSelection(
        workspace_id=workspace_id,
        root=root,
        name="Primary",
    )


def _insert_project(connection, *, name="Alpha", path=None):
    connection.execute(
        "INSERT INTO projects (id, name, path) VALUES ('p1', ?, ?)",
        (name, path),
    )
    connection.commit()


def _register_repo(connection, name: str, path: Path):
    connection.execute(
        "INSERT INTO project_repo_policies VALUES ('p1', ?, ?, NULL, 1)",
        (name, str(path.resolve())),
    )
    connection.commit()


def test_config_exposes_loaded_workspace_selection():
    from infra import config

    assert isinstance(config.WORKSPACE_SELECTION, WorkspaceSelection)
    assert config.WORKSPACE_SELECTION.root == config.WORKSPACE


def test_logical_project_path_resolves_as_relocatable_planning_root(
    tmp_path,
    project_db,
):
    selected = tmp_path / "selected"
    planning = selected / "projects" / "alpha"
    planning.mkdir(parents=True)
    _insert_project(
        project_db,
        path="workspace://workspace-123/projects/alpha",
    )

    assert resolve_project_paths(
        selected,
        "p1",
        db=project_db,
        selection=_selection(selected),
    ) == ("Alpha", planning.resolve())


@pytest.mark.parametrize("path", [None, "", "/srv/code/engine"])
def test_kept_legacy_project_path_resolves_to_its_project_folder(tmp_path, project_db, path):
    selected = tmp_path / "selected"
    planning = selected / "projects" / "alpha"
    planning.mkdir(parents=True)
    _insert_project(project_db, path=path)

    assert resolve_project_paths(
        selected,
        "p1",
        db=project_db,
        selection=_selection(selected),
    ) == ("Alpha", planning.resolve())


@pytest.mark.parametrize("path", ["/srv/code/engine", "relative/engine"])
def test_legacy_project_path_without_folder_has_no_planning_root(
    tmp_path,
    project_db,
    path,
):
    selected = tmp_path / "selected"
    (selected / "projects").mkdir(parents=True)
    _insert_project(project_db, path=path)

    assert resolve_project_paths(
        selected,
        "p1",
        db=project_db,
        selection=_selection(selected),
    ) == ("Alpha", None)


def test_unknown_project_resolves_to_nothing(tmp_path, project_db):
    assert resolve_project_paths(tmp_path, "missing", db=project_db) == (None, None)


def test_logical_project_reference_is_explicit_even_when_directory_is_missing(
    tmp_path,
    project_db,
):
    selected = tmp_path / "selected"
    selected.mkdir()
    expected = selected / "projects" / "alpha"
    _insert_project(
        project_db,
        path="workspace://workspace-123/projects/alpha",
    )

    assert resolve_project_paths(
        selected,
        "p1",
        db=project_db,
        selection=_selection(selected),
    ) == ("Alpha", expected.resolve(strict=False))


def test_invalid_logical_project_reference_fails_loudly(tmp_path, project_db):
    selected = tmp_path / "selected"
    selected.mkdir()
    _insert_project(
        project_db,
        path="workspace://wrong-workspace/projects/alpha",
    )

    with pytest.raises(ValueError, match="workspace id"):
        resolve_project_paths(
            selected,
            "p1",
            db=project_db,
            selection=_selection(selected),
        )


def test_explicit_selection_must_match_workspace_argument(tmp_path, project_db):
    workspace = tmp_path / "workspace-a"
    other = tmp_path / "workspace-b"
    workspace.mkdir()
    other.mkdir()
    _insert_project(
        project_db,
        path="workspace://workspace-123/projects/alpha",
    )

    with pytest.raises(ValueError, match="selected root"):
        resolve_project_paths(
            workspace,
            "p1",
            db=project_db,
            selection=_selection(other),
        )


def test_project_context_loads_only_logical_planning_root_context(
    tmp_path,
    project_db,
    monkeypatch,
):
    from core.project_context import project_context_source_entries
    from infra import config

    selected = tmp_path / "selected"
    planning = selected / "projects" / "alpha"
    external = tmp_path / "code" / "engine"
    (planning / "context").mkdir(parents=True)
    (external / "context").mkdir(parents=True)
    (planning / "context" / "PROJECT.md").write_text("PLANNING", encoding="utf-8")
    (planning / "context" / "COORDINATION-MODEL.md").write_text(
        "PLANNING COORDINATION", encoding="utf-8"
    )
    (external / "context" / "REPO.md").write_text("REPO", encoding="utf-8")
    _insert_project(
        project_db,
        path="workspace://workspace-123/projects/alpha",
    )
    monkeypatch.setattr(config, "WORKSPACE", selected)
    monkeypatch.setattr(
        config, "WORKSPACE_SELECTION", _selection(selected), raising=False
    )

    entries = project_context_source_entries(
        selected,
        "p1",
        db=project_db,
    )
    rendered = "\n".join(entry.prompt_content or "" for entry in entries)

    assert "PLANNING" in rendered
    assert "REPO" not in rendered


def test_files_roots_uses_planning_root_and_policy_registered_repos_only(
    tmp_path,
    project_db,
    monkeypatch,
):
    from api import files_roots
    from infra import config

    selected = tmp_path / "selected"
    planning = selected / "projects" / "alpha"
    repo = tmp_path / "code" / "engine"
    rogue = tmp_path / "code" / "rogue"
    (planning / "source").mkdir(parents=True)
    repo.mkdir(parents=True)
    rogue.mkdir(parents=True)
    (planning / "source" / "engine").symlink_to(repo, target_is_directory=True)
    (planning / "source" / "rogue").symlink_to(rogue, target_is_directory=True)
    _insert_project(
        project_db,
        path="workspace://workspace-123/projects/alpha",
    )
    _register_repo(project_db, "engine", repo)
    monkeypatch.setattr(config, "WORKSPACE", selected)
    monkeypatch.setattr(
        config, "WORKSPACE_SELECTION", _selection(selected), raising=False
    )

    resolved_planning, repos = files_roots.resolve_project("p1", project_db)

    assert resolved_planning == planning.resolve()
    assert repos == [("engine", repo.resolve())]


def test_repo_policy_keeps_unresolved_planning_binding_fail_closed(
    tmp_path,
    project_db,
    monkeypatch,
):
    from infra import project_repo_policy

    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_project(
        project_db,
        path="workspace://workspace-123/projects/alpha",
    )
    _register_repo(project_db, "engine", repo)
    monkeypatch.setattr(
        project_repo_policy,
        "resolve_project_paths",
        lambda *_args, **_kwargs: ("Alpha", None),
        raising=False,
    )

    policies = project_repo_policy.list_registered_repo_policies(
        workspace=tmp_path / "selected",
        db=project_db,
    )

    assert len(policies) == 1
    assert policies[0].path == repo.resolve()
    assert policies[0].source_binding_valid is False
    assert "planning root" in (policies[0].enforcement_error or "")
