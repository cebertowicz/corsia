from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.project_context import resolve_project_paths
from infra.platform_paths import WorkspaceSelection
from infra.workspace_paths import WorkspaceReference


def _selection(root: Path, workspace_id: str = "workspace-123") -> WorkspaceSelection:
    return WorkspaceSelection(
        workspace_id=workspace_id,
        root=root,
        name="Primary",
    )


@pytest.fixture
def project_db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT)"
    )
    yield connection
    connection.close()


def _insert_project(connection):
    connection.execute("INSERT INTO projects VALUES ('p1', 'Alpha', NULL)")
    connection.commit()


def test_workspace_reference_round_trips_workspace_root(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    selection = _selection(workspace)

    reference = WorkspaceReference.from_path(workspace, selection)

    assert reference.serialize() == "workspace://workspace-123/"
    assert WorkspaceReference.parse(reference.serialize()) == reference
    assert reference.resolve(selection) == workspace.resolve()


def test_workspace_reference_round_trips_spaces_and_unicode(tmp_path):
    workspace = tmp_path / "workspace"
    target = workspace / "shared" / "Résumé notes.md"
    target.parent.mkdir(parents=True)
    target.write_text("notes", encoding="utf-8")
    selection = _selection(workspace)

    serialized = WorkspaceReference.from_path(target, selection).serialize()

    assert serialized == (
        "workspace://workspace-123/shared/R%C3%A9sum%C3%A9%20notes.md"
    )
    assert WorkspaceReference.parse(serialized).resolve(selection) == target.resolve()


def test_workspace_reference_relocates_with_stable_workspace_identity(tmp_path):
    original = tmp_path / "original"
    relocated = tmp_path / "relocated"
    source = original / "projects" / "alpha"
    target = relocated / "projects" / "alpha"
    source.mkdir(parents=True)
    target.mkdir(parents=True)

    reference = WorkspaceReference.from_path(source, _selection(original))

    assert reference.resolve(_selection(relocated)) == target.resolve()


def test_workspace_reference_rejects_wrong_selected_workspace(tmp_path):
    reference = WorkspaceReference.parse("workspace://workspace-123/projects/alpha")

    with pytest.raises(ValueError, match="workspace id"):
        reference.resolve(_selection(tmp_path / "other", "workspace-456"))


def test_workspace_reference_rejects_paths_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()

    with pytest.raises(ValueError, match="outside"):
        WorkspaceReference.from_path(outside, _selection(workspace))


def test_workspace_reference_rejects_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    selection = _selection(workspace)

    with pytest.raises(ValueError, match="outside"):
        WorkspaceReference.from_path(workspace / "escape" / "secret.txt", selection)

    reference = WorkspaceReference.parse("workspace://workspace-123/escape/secret.txt")
    with pytest.raises(ValueError, match="outside"):
        reference.resolve(selection)


@pytest.mark.parametrize(
    "raw",
    [
        "workspace://workspace-123/projects/../secret",
        "workspace://workspace-123/projects/%2e%2e/secret",
        "workspace://workspace-123/./secret",
        "workspace://workspace-123/%2E/secret",
    ],
)
def test_workspace_reference_rejects_raw_and_encoded_traversal(raw):
    with pytest.raises(ValueError, match="traversal"):
        WorkspaceReference.parse(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "workspace://workspace-123/shared%2Fsecret",
        "workspace://workspace-123/shared%5csecret",
        r"workspace://workspace-123/shared\secret",
    ],
)
def test_workspace_reference_rejects_encoded_or_raw_separators(raw):
    with pytest.raises(ValueError, match="separator"):
        WorkspaceReference.parse(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "/absolute/path",
        "relative/path",
        "file:///absolute/path",
        "workspace://workspace-123",
        "workspace://workspace-123//shared",
        "workspace://workspace-123/shared/%zz",
        "workspace://workspace-123/shared/file.md?revision=2",
        "workspace://workspace-123/shared/file.md#section",
    ],
)
def test_workspace_reference_parse_accepts_only_canonical_references(raw):
    with pytest.raises(ValueError):
        WorkspaceReference.parse(raw)


def test_workspace_selection_for_root_reuses_loaded_runtime_selection(
    tmp_path,
    monkeypatch,
):
    from infra import config, workspace_paths

    workspace = tmp_path / "workspace"
    selection = _selection(workspace)
    monkeypatch.setattr(config, "WORKSPACE", workspace.resolve())
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", selection, raising=False)

    assert workspace_paths.workspace_selection_for_root(workspace) is selection


@pytest.mark.parametrize(
    "logical",
    [
        "workspace://workspace-123/",
        "workspace://workspace-123/shared",
        "workspace://workspace-123/projects/alpha/context",
    ],
)
def test_logical_project_reference_must_name_direct_projects_child(
    logical,
    tmp_path,
    project_db,
):
    selected = tmp_path / "selected"
    selected.mkdir()
    project_db.execute("INSERT INTO projects VALUES ('p1', 'Alpha', ?)", (logical,))
    project_db.commit()

    with pytest.raises(ValueError, match="direct child"):
        resolve_project_paths(
            selected,
            "p1",
            db=project_db,
            selection=_selection(selected),
        )


def test_logical_project_reference_rejects_cross_project_symlink(
    tmp_path,
    project_db,
):
    selected = tmp_path / "selected"
    beta = selected / "projects" / "beta"
    beta.mkdir(parents=True)
    (selected / "projects" / "alpha").symlink_to(beta, target_is_directory=True)
    project_db.execute(
        "INSERT INTO projects VALUES "
        "('p1', 'Alpha', 'workspace://workspace-123/projects/alpha')"
    )
    project_db.commit()

    with pytest.raises(ValueError, match="symlink"):
        resolve_project_paths(
            selected,
            "p1",
            db=project_db,
            selection=_selection(selected),
        )


def test_project_repo_workspace_lookup_delegates_to_canonical_resolver(
    tmp_path,
    project_db,
    monkeypatch,
):
    from api import project_repos

    planning = tmp_path / "selected" / "projects" / "alpha"
    planning.mkdir(parents=True)
    _insert_project(project_db)
    monkeypatch.setattr(project_repos, "get_db", lambda: project_db)
    calls = []

    def resolve(workspace, project_id, *, db=None, selection=None):
        calls.append((workspace, project_id, db))
        return "Alpha", planning

    monkeypatch.setattr(project_repos, "resolve_project_paths", resolve, raising=False)

    assert project_repos._workspace_dir_for("p1") == planning
    assert calls == [(project_repos.config.WORKSPACE, "p1", project_db)]


def test_skills_registry_delegates_project_planning_root_resolution(
    tmp_path,
    project_db,
    monkeypatch,
):
    from core import project_context
    from core.skills_registry import _project_dir

    planning = tmp_path / "selected" / "projects" / "custom-root"
    _insert_project(project_db)
    monkeypatch.setattr(
        project_context,
        "resolve_project_paths",
        lambda *_args, **_kwargs: ("Alpha", planning),
    )

    assert _project_dir("p1", tmp_path / "selected", project_db) == planning


def test_project_api_projects_logical_path_without_changing_stored_truth(
    tmp_path,
    monkeypatch,
):
    from api import projects

    planning = tmp_path / "selected" / "projects" / "alpha"
    row = {
        "id": "p1",
        "name": "Alpha",
        "path": "workspace://workspace-123/projects/alpha",
    }
    monkeypatch.setattr(
        projects,
        "resolve_project_paths",
        lambda *_args, **_kwargs: ("Alpha", planning),
    )

    projected = projects._project_api_projection(row, db=object())

    assert projected["path"] == str(planning)
    assert row["path"] == "workspace://workspace-123/projects/alpha"


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    [
        ("infra.fresh_threads", "resolve_spawn_working_dir"),
        ("subagents.cc_runtime_helpers", "_main_working_dir"),
    ],
)
def test_cc_and_fresh_thread_cwd_delegate_to_planning_root(
    module_name,
    function_name,
    tmp_path,
    project_db,
    monkeypatch,
):
    import importlib

    module = importlib.import_module(module_name)
    planning = tmp_path / "selected" / "projects" / "alpha"
    _insert_project(project_db)
    if module_name == "infra.fresh_threads":
        project_db.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, project_id TEXT)"
        )
        project_db.execute("INSERT INTO threads VALUES ('parent', 'p1')")
        project_db.commit()
    monkeypatch.setattr(
        module,
        "resolve_project_paths",
        lambda *_args, **_kwargs: ("Alpha", planning),
        raising=False,
    )

    argument = "parent" if module_name == "infra.fresh_threads" else "p1"
    result = getattr(module, function_name)(argument, db=project_db)

    assert result == (
        (str(planning), "p1")
        if module_name == "infra.fresh_threads"
        else str(planning)
    )


def test_codex_cwd_delegates_to_planning_root(
    tmp_path,
    project_db,
    monkeypatch,
):
    from core import project_context
    from subagents.codex_runtime_support import resolve_working_dir

    planning = tmp_path / "selected" / "projects" / "alpha"
    _insert_project(project_db)
    monkeypatch.setattr(
        project_context,
        "resolve_project_paths",
        lambda *_args, **_kwargs: ("Alpha", planning),
    )

    assert resolve_working_dir("p1", project_db) == str(planning)


@pytest.mark.parametrize(
    ("value", "leaf"),
    [
        ("workspace://workspace-123/projects/my%20project", "my project"),
        ("workspace://workspace-123/projects/caf%C3%A9", "café"),
        ("workspace://workspace-123/", None),
        ("workspace://workspace-123/projects/bad%ZZ", None),
        ("/Users/me/projects/alpha", None),
        (None, None),
    ],
)
def test_reference_leaf_uses_the_canonical_decoder(value, leaf):
    from infra.workspace_paths import reference_leaf

    assert reference_leaf(value) == leaf
