from __future__ import annotations

import json
from pathlib import Path

import pytest

from infra.platform_paths import WorkspaceSelection
from tests.registry_helpers import make_session, seed_artifact


@pytest.fixture
def identity_scope(isolated_db, tmp_path, monkeypatch):
    from infra import config
    from infra.database import get_db
    from registry import service

    workspace = tmp_path / "workspace"
    repository = tmp_path / "repository"
    workspace.mkdir()
    repository.mkdir()
    selection = WorkspaceSelection(
        workspace_id="workspace-stable-id",
        root=workspace.resolve(),
        name="Test workspace",
    )
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", selection)
    monkeypatch.setattr(
        service,
        "get_allowed_dirs_rw",
        lambda **_kwargs: [workspace.resolve(), repository.resolve()],
    )
    db = get_db()
    thread_id = make_session(db, prefix="identity")
    return workspace, repository, thread_id, db


def test_workspace_write_uses_logical_identity_and_physical_path(identity_scope):
    from registry import service

    workspace, _repository, thread_id, db = identity_scope
    path = workspace / "shared" / "planning" / "roadmap.md"

    artifact = service.register_write_strict(
        thread_id,
        str(path),
        "Current roadmap",
        db=db,
    )

    assert artifact["external_id"] == (
        "workspace://workspace-stable-id/shared/planning/roadmap.md"
    )
    assert artifact["path"] == str(path.resolve())
    assert service.resolve_file_identity(str(path), db=db)["id"] == artifact["id"]


def test_repository_write_keeps_absolute_identity(identity_scope):
    from registry import service

    _workspace, repository, thread_id, db = identity_scope
    path = repository / "README.md"

    artifact = service.register_write_strict(thread_id, str(path), db=db)

    assert artifact["external_id"] == str(path.resolve())
    assert artifact["path"] == str(path.resolve())


def test_native_repository_inside_workspace_keeps_absolute_identity(
    identity_scope,
    monkeypatch,
):
    from registry import service

    workspace, repository, thread_id, db = identity_scope
    native_repository = workspace / "projects" / "alpha" / "source" / "native"
    native_repository.mkdir(parents=True)
    monkeypatch.setattr(
        service,
        "get_allowed_dirs_rw",
        lambda **_kwargs: [
            workspace.resolve(),
            native_repository.resolve(),
            repository.resolve(),
        ],
    )
    path = native_repository / "README.md"

    artifact = service.register_write_strict(thread_id, str(path), db=db)

    assert artifact["external_id"] == str(path.resolve())
    assert artifact["path"] == str(path.resolve())
    assert json.loads(artifact["metadata"])["zone"] == "repo"


def test_workspace_inside_repository_keeps_logical_identity(
    identity_scope,
    monkeypatch,
):
    from registry import service

    workspace, _repository, thread_id, db = identity_scope
    ancestor_repository = workspace.parent
    monkeypatch.setattr(
        service,
        "get_allowed_dirs_rw",
        lambda **_kwargs: [workspace.resolve(), ancestor_repository.resolve()],
    )
    path = workspace / "shared" / "planning" / "inside-workspace.md"

    artifact = service.register_write_strict(thread_id, str(path), db=db)

    assert artifact["external_id"] == (
        "workspace://workspace-stable-id/shared/planning/inside-workspace.md"
    )
    assert json.loads(artifact["metadata"])["zone"] == "workspace"


def test_repository_ancestor_outside_workspace_keeps_absolute_identity(
    identity_scope,
    monkeypatch,
):
    from registry import service

    workspace, _repository, thread_id, db = identity_scope
    ancestor_repository = workspace.parent
    monkeypatch.setattr(
        service,
        "get_allowed_dirs_rw",
        lambda **_kwargs: [workspace.resolve(), ancestor_repository.resolve()],
    )
    path = ancestor_repository / "outside-workspace.md"

    artifact = service.register_write_strict(thread_id, str(path), db=db)

    assert artifact["external_id"] == str(path.resolve())
    assert json.loads(artifact["metadata"])["zone"] == "repo"


@pytest.mark.parametrize("legacy_state", ["active", "archived"])
def test_legacy_workspace_identity_is_reused_and_upgraded(
    identity_scope,
    legacy_state,
):
    from registry import service

    workspace, _repository, thread_id, db = identity_scope
    path = workspace / "shared" / "reference" / "facts.md"
    absolute = str(path.resolve())
    seed_artifact(
        db,
        artifact_id="legacy-id",
        integration="workspace",
        type="document",
        external_id=absolute,
        parent_thread_id=thread_id,
        path=absolute,
        state=legacy_state,
    )
    db.commit()

    artifact = service.register_write_strict(thread_id, absolute, db=db)

    assert artifact["id"] == "legacy-id"
    assert artifact["external_id"].startswith("workspace://workspace-stable-id/")
    rows = db.execute(
        "SELECT id,external_id,state FROM artifact_identities "
        "WHERE integration='workspace' AND type='document'"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("legacy-id", artifact["external_id"], "active")
    ]


def test_logical_identity_wins_when_legacy_duplicate_already_exists(identity_scope):
    from registry import service

    workspace, _repository, thread_id, db = identity_scope
    path = workspace / "shared" / "planning" / "same.md"
    absolute = str(path.resolve())
    logical = "workspace://workspace-stable-id/shared/planning/same.md"
    seed_artifact(
        db,
        artifact_id="logical-id",
        integration="workspace",
        type="document",
        external_id=logical,
        parent_thread_id=thread_id,
        path=absolute,
    )
    seed_artifact(
        db,
        artifact_id="legacy-id",
        integration="workspace",
        type="document",
        external_id=absolute,
        parent_thread_id=thread_id,
        path=absolute,
    )
    db.commit()

    artifact = service.register_write_strict(thread_id, absolute, db=db)

    assert artifact["id"] == "logical-id"
    assert db.execute("SELECT COUNT(*) FROM artifact_identities").fetchone()[0] == 2


def test_strict_registration_participates_in_caller_transaction(identity_scope):
    from registry import service

    workspace, _repository, thread_id, db = identity_scope
    path = workspace / "shared" / "planning" / "rollback.md"

    db.execute("BEGIN")
    service.register_write_strict(
        thread_id,
        str(path),
        db=db,
    )
    db.rollback()

    assert db.execute("SELECT COUNT(*) FROM artifact_identities").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM artifact_registrations").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_registration_events"
    ).fetchone()[0] == 0


def test_relative_registration_reuses_caller_db_for_repo_policy_lookup(
    identity_scope,
    monkeypatch,
):
    from registry import service

    workspace, _repository, thread_id, db = identity_scope

    lookups = []

    def allowed_dirs(**kwargs):
        lookups.append(kwargs.get("db"))
        return [workspace.resolve()]

    monkeypatch.setattr(service, "get_allowed_dirs_rw", allowed_dirs)
    db.execute("BEGIN")
    artifact = service.register_write_strict(
        thread_id,
        "shared/planning/relative.md",
        db=db,
    )

    assert artifact["path"] == str(
        (workspace / "shared" / "planning" / "relative.md").resolve()
    )
    assert lookups
    assert all(lookup is db for lookup in lookups)
    assert db.in_transaction
    db.rollback()


def test_best_effort_registration_still_swallows_strict_failure(
    identity_scope,
    monkeypatch,
):
    from registry import service

    workspace, _repository, thread_id, db = identity_scope

    def fail(*_args, **_kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(service, "register_write_strict", fail)

    assert service.register_write(
        thread_id,
        str(workspace / "notes.md"),
        db=db,
        notify=False,
    ) is None
