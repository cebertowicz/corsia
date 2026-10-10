"""Public workspace/project/thread layout resolver contracts."""

from __future__ import annotations

import sqlite3

import pytest

from tests.project_ref_helpers import project_ref, select_workspace


def _project_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT)")
    return db


def test_resolvers_canonicalize_scope_and_select_project_root(tmp_path, monkeypatch):
    from core.workspace_layout import (
        resolve_project_root,
        resolve_thread_root,
        resolve_thread_uploads_root,
        resolve_thread_working_root,
        resolve_workspace_root,
    )

    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "alpha"
    project.mkdir(parents=True)
    select_workspace(monkeypatch, workspace)
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    db = _project_db()
    db.execute(
        "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
        ("p1", "Alpha", project_ref("alpha")),
    )

    assert resolve_workspace_root(alias) == workspace.resolve()
    assert resolve_project_root(alias, "p1", db=db) == project.resolve()
    assert resolve_thread_root(project, "thread-1") == project / ".threads" / "thread-1"
    assert resolve_thread_working_root(project, "thread-1") == (
        project / ".threads" / "thread-1" / "working"
    )
    assert resolve_thread_uploads_root(project, "thread-1") == (
        project / ".threads" / "thread-1" / "uploads"
    )
    db.close()


def test_project_root_stays_unresolved_without_a_resolved_project_scope(tmp_path):
    from core.workspace_layout import resolve_project_root

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = _project_db()
    db.execute("INSERT INTO projects (id, name) VALUES (?, ?)", ("p1", "Missing"))

    assert resolve_project_root(workspace, "p1", db=db) is None
    db.close()


@pytest.mark.parametrize("thread_id", ["", " ", ".", "..", "a/b", r"a\\b", "nul\x00id"])
def test_thread_root_rejects_unsafe_thread_identities(tmp_path, thread_id):
    from core.workspace_layout import resolve_thread_root

    with pytest.raises(ValueError):
        resolve_thread_root(tmp_path, thread_id)


def test_thread_root_rejects_symlinked_destination_or_parent(tmp_path):
    from core.workspace_layout import resolve_thread_root

    outside = tmp_path / "outside"
    outside.mkdir()
    threads = tmp_path / ".threads"
    threads.mkdir()
    (threads / "thread-1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        resolve_thread_root(tmp_path, "thread-1")

    parent_scope = tmp_path / "parent-scope"
    parent_scope.mkdir()
    (parent_scope / ".threads").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        resolve_thread_root(parent_scope, "thread-2")


def test_thread_root_rejects_a_non_directory_destination(tmp_path):
    from core.workspace_layout import resolve_thread_root

    destination = tmp_path / ".threads" / "thread-1"
    destination.parent.mkdir()
    destination.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        resolve_thread_root(tmp_path, "thread-1")


def test_thread_working_root_rejects_a_non_directory_working_tray(tmp_path):
    from core.workspace_layout import resolve_thread_working_root

    working = tmp_path / ".threads" / "thread-1" / "working"
    working.parent.mkdir(parents=True)
    working.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        resolve_thread_working_root(tmp_path, "thread-1")


def test_thread_uploads_root_rejects_a_symlink_or_non_directory(tmp_path):
    from core.workspace_layout import resolve_thread_uploads_root

    uploads = tmp_path / ".threads" / "thread-1" / "uploads"
    uploads.parent.mkdir(parents=True)
    uploads.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        resolve_thread_uploads_root(tmp_path, "thread-1")

    uploads.unlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    uploads.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        resolve_thread_uploads_root(tmp_path, "thread-1")


def test_thread_root_resolver_inspects_a_scope_once_and_each_thread_once(tmp_path, monkeypatch):
    from core import workspace_layout

    (tmp_path / ".threads").mkdir()
    for index in range(30):
        (tmp_path / ".threads" / f"thread-{index}").mkdir()
    scopes: list[str] = []
    destinations: list[str] = []
    resolve_scope = workspace_layout.resolve_workspace_root
    lstat = workspace_layout.os.lstat
    monkeypatch.setattr(
        workspace_layout, "resolve_workspace_root", lambda scope: scopes.append(str(scope)) or resolve_scope(scope)
    )
    monkeypatch.setattr(
        workspace_layout.os, "lstat", lambda path, *a, **k: destinations.append(str(path)) or lstat(path, *a, **k)
    )

    resolver = workspace_layout.ThreadRootResolver()
    roots = [resolver.root(tmp_path, f"thread-{index}") for index in range(30)]

    assert roots[0] == tmp_path.resolve() / ".threads" / "thread-0"
    assert roots[29] == tmp_path.resolve() / ".threads" / "thread-29"
    assert scopes == [str(tmp_path)]
    assert [path.rsplit("/", 1)[1] for path in destinations if "/.threads/thread-" in path] == [
        f"thread-{index}" for index in range(30)
    ]


def test_thread_root_resolver_reports_a_bad_container_on_every_thread(tmp_path):
    from core.workspace_layout import ThreadRootResolver

    (tmp_path / ".threads").write_text("not a directory", encoding="utf-8")
    resolver = ThreadRootResolver()

    for thread_id in ("thread-1", "thread-2"):
        with pytest.raises(ValueError, match="container is not a directory"):
            resolver.root(tmp_path, thread_id)


def test_thread_root_resolver_returns_a_missing_destination_unchanged(tmp_path):
    from core.workspace_layout import ThreadRootResolver

    assert ThreadRootResolver().root(tmp_path, "thread-1") == tmp_path.resolve() / ".threads" / "thread-1"
