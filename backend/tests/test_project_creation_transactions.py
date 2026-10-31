"""Project filesystem preparation must not occupy SQLite's shared writer."""

from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

from pathlib import Path
import sqlite3

import pytest

pytestmark = pytest.mark.usefixtures('signed_in_defaults')


@pytest.fixture
def project_environment(isolated_db, tmp_path, monkeypatch):
    from api import project_scaffolding
    from infra.database import get_db
    from infra.platform_paths import WorkspaceSelection

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(project_scaffolding.config, "WORKSPACE", workspace)
    monkeypatch.setattr(
        project_scaffolding.config, "WORKSPACE_SELECTION",
        WorkspaceSelection(workspace_id="creation", root=workspace, name="Creation"),
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    db = get_db()
    db.execute("CREATE TABLE creation_probe (value TEXT)")
    peer = sqlite3.connect(isolated_db, timeout=0.01)
    try:
        yield db, peer, workspace, repo
    finally:
        if db.in_transaction:
            db.rollback()
        peer.close()
        db.close()


def test_initialize_project_designates_project_conductor(project_environment):
    """Project creation uses the caller-owned user id and stated model for its conductor."""
    from api import project_scaffolding
    from infra import accounts
    from infra.conductor import get_scope_coordinator

    db, _peer, _workspace, _repo = project_environment
    accounts.create_user("owner", "long-enough-password", db=db)

    project = project_scaffolding.initialize_project(
        db, name="Created", user_id="owner", conductor_model="claude-code/sonnet",
    )

    conductor_id = get_project_conductor(project["id"], db=db)
    row = db.execute(
        "SELECT user_id,title,project_id,model FROM threads WHERE id=?",
        (conductor_id,),
    ).fetchone()
    assert dict(row) == {
        "user_id": "owner",
        "title": "Created Conductor",
        "project_id": project["id"],
        "model": "claude-code/sonnet",
    }


def test_initialize_project_enforces_its_fresh_scaffolded_shared_tree(project_environment):
    from api import project_scaffolding
    from infra import accounts
    from infra.conductor import get_scope_coordinator
    from infra.settings import get_setting
    from registry import service
    from registry.shared_principals import artifact_owner

    db, _peer, workspace, _repo = project_environment
    accounts.create_user("owner", "long-enough-password", db=db)

    project = project_scaffolding.initialize_project(
        db, name="Created", user_id="owner", conductor_model=None,
    )

    conductor = get_project_conductor(project["id"], db=db)
    shared_index = workspace / "projects" / "created" / "shared" / "INDEX.md"
    artifact = service.find_file_identity(str(shared_index), db=db)
    assert get_setting(f"shared_ownership:{project['id']}", db=db)
    assert artifact is not None
    assert artifact_owner(str(artifact["id"]), db=db) == conductor
    assert db.execute(
        "SELECT 1 FROM plan_nodes WHERE title=?",
        ("Claim your shared documents: keep, hand off, or archive",),
    ).fetchone() is None


@pytest.mark.parametrize("boundary", (
    "_scaffold_project_workspace", "link_repo_into_source", "compute_doc_roots",
    "resolve_repository",
))
def test_filesystem_preparation_allows_an_unrelated_writer(
    project_environment, monkeypatch, boundary,
):
    """A real second writer can commit while each filesystem stage runs."""
    from api import project_scaffolding

    db, peer, workspace, repo = project_environment
    if boundary == "resolve_repository":
        target, name = Path, "resolve"
    else:
        target, name = project_scaffolding, boundary
    original = getattr(target, name)

    def prepare(*args, **kwargs):
        if boundary != "resolve_repository" or args[0] == repo:
            peer.execute("INSERT INTO creation_probe VALUES (?)", (boundary,))
            peer.commit()
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, prepare)
    project = project_scaffolding.initialize_project(
        db, name="Created", repo_path_str=str(repo), user_id="owner", conductor_model=None,
    )

    assert peer.execute("SELECT COUNT(*) FROM creation_probe").fetchone()[0] > 0
    assert project["path"] == "workspace://creation/projects/created"
    assert (workspace / "projects/created/source/repo").is_symlink()
    assert tuple(peer.execute(
        "SELECT repo_name,repo_path,pinned_branch,protect_base_checkout "
        "FROM project_repo_policies WHERE project_id=?", (project["id"],),
    ).fetchone()) == ("repo", str(repo), None, 0)


@pytest.mark.parametrize("scaffold_fails", (False, True))
def test_foreign_transaction_is_preserved_before_any_filesystem_effect(
    project_environment, monkeypatch, scaffold_fails,
):
    """Creation cannot commit or discard work belonging to its caller."""
    from api import project_scaffolding
    from infra.database import TransactionOwnershipError

    db, peer, workspace, repo = project_environment
    db.execute("INSERT INTO creation_probe VALUES ('caller')")
    if scaffold_fails:
        def fail(*_args, **_kwargs):
            raise RuntimeError("scaffold failed")

        monkeypatch.setattr(project_scaffolding, "_scaffold_project_workspace", fail)

    with pytest.raises(TransactionOwnershipError, match="clean"):
        project_scaffolding.initialize_project(
            db, name="Created", repo_path_str=str(repo), user_id="owner", conductor_model=None,
        )

    assert db.in_transaction
    assert db.execute("SELECT value FROM creation_probe").fetchall()[0][0] == "caller"
    assert peer.execute("SELECT COUNT(*) FROM creation_probe").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    assert not (workspace / "projects").exists()


@pytest.mark.parametrize("failed_write", ("policy", "doc_roots"))
def test_failed_project_write_rolls_back_before_owned_scaffold_cleanup(
    project_environment, monkeypatch, failed_write,
):
    """Coupled rows roll back together and compensation holds no DB writer."""
    from api import project_scaffolding

    db, peer, workspace, repo = project_environment
    if failed_write == "policy":
        db.execute(
            "CREATE TRIGGER fail_creation BEFORE INSERT ON project_repo_policies "
            "BEGIN SELECT RAISE(ABORT, 'creation SQL failed'); END"
        )
    else:
        db.execute(
            "CREATE TRIGGER fail_creation BEFORE UPDATE OF doc_roots ON projects "
            "BEGIN SELECT RAISE(ABORT, 'creation SQL failed'); END"
        )
    original = project_scaffolding.remove_owned_scaffold

    def cleanup(ownership):
        peer.execute("INSERT INTO creation_probe VALUES ('cleanup')")
        peer.commit()
        return original(ownership)

    monkeypatch.setattr(project_scaffolding, "remove_owned_scaffold", cleanup)
    with pytest.raises(sqlite3.IntegrityError, match="creation SQL failed"):
        project_scaffolding.initialize_project(
            db, name="Created", repo_path_str=str(repo), user_id="owner", conductor_model=None,
        )

    assert not db.in_transaction
    assert peer.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    assert peer.execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 0
    assert peer.execute("SELECT value FROM creation_probe").fetchall() == [("cleanup",)]
    assert not (workspace / "projects/created").exists()
    assert repo.is_dir()
