"""Bringing an earlier CORSIA install over: ``corsia import`` and its route (issue #2125).

A dry run writes nothing, ``--apply`` lands each item through the writer that
already owns it, nothing is ever overwritten, and screened content never comes
over. The new modules are imported inside each test, so the route-registration
test below fails on a tree without the feature instead of failing collection.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.auth_fixtures import (
    OWNER_SESSION_TOKEN, OWNER_TOKEN, OWNER_USER, seed_account,
)

pytestmark = pytest.mark.usefixtures("signed_in_defaults")

SERVICE_TOKEN = "service-token-for-import-tests"

WS_SKILL = """---
name: ws-skill
description: A workspace skill under test.
---

# Workspace skill

Body.
"""

ALPHA_SKILL = """---
name: alpha-skill
description: A project skill under test.
---

# Project skill

Body.
"""

# A recognized credential shape, so the screening leaves this file behind.
SECRET_NOTE = "# Keys\n\nAWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"


def _git(workspace: Path, *args: str) -> None:
    done = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=T", *args],
        cwd=str(workspace), capture_output=True, text=True, timeout=30,
    )
    assert done.returncode == 0, done.stderr


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _earlier(root: Path) -> Path:
    """A synthetic earlier workspace: two projects, plus what must stay behind."""
    _write(root / "context" / "50-USER-PROFILE.md", "# Profile\n\nWorkspace context stays.\n")
    _write(root / "skills" / "ws-skill" / "SKILL.md", WS_SKILL)
    _write(root / "shared" / "notes" / "INDEX.md", "# Notes\n\nShared notes.\n")
    _write(root / "shared" / "notes" / "plan.md", "# Plan\n\nThe plan.\n")
    _write(root / "shared" / "notes" / "keys.md", SECRET_NOTE)
    _write(root / ".env", "PROVIDER_API_KEY=sk-not-a-real-key\n")
    _write(root / ".corsia" / "config.json", "{}\n")
    _write(root / "corsia.db", "not a real database\n")
    _write(root / ".threads" / "abc" / "working" / "scratch.md", "# Scratch\n")
    _write(root / "projects" / "alpha" / "context" / "PROJECT.md", "# Alpha Project\n\nPurpose.\n")
    _write(root / "projects" / "alpha" / "context" / "COORDINATION-MODEL.md", "# Alpha — model\n\nRoutes.\n")
    _write(root / "projects" / "alpha" / "skills" / "alpha-skill" / "SKILL.md", ALPHA_SKILL)
    _write(root / "projects" / "alpha" / "shared" / "product" / "INDEX.md", "# Product\n\nFacts.\n")
    _write(root / "projects" / "alpha" / "shared" / "product" / "facts.md", "# Facts\n\nOne fact.\n")
    _write(root / "projects" / "alpha" / "source" / "repo" / "code.py", "SECRET = 1\n")
    _write(root / "projects" / "alpha" / ".threads" / "x" / "note.md", "# Note\n")
    _write(root / "projects" / "beta" / "context" / "PROJECT.md", "# Beta\n\nPurpose.\n")
    _write(root / "projects" / "beta" / "shared" / "INDEX.md", "# Beta shared\n\nBeta's own blurb.\n")
    (root / "shared" / "notes" / "outside.md").symlink_to(root.parent / "elsewhere.md")
    _write(root.parent / "elsewhere.md", "# Elsewhere\n\nOutside the folder.\n")
    return root


@pytest.fixture
def install(isolated_db, tmp_path, monkeypatch):
    """A scratch workspace with a CORSIA agent, and an earlier workspace beside it."""
    from core.workspace_scaffolding import scaffold_workspace
    from infra import config
    from infra.corsia_agent import ensure_corsia_agent
    from infra.database import get_db
    from infra.platform_paths import WorkspaceSelection

    workspace = tmp_path / "workspace"
    scaffold_workspace(workspace)
    _git(workspace, "init", "--quiet")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-qm", "scaffold")
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(
        config, "WORKSPACE_SELECTION",
        WorkspaceSelection("earlier-install-tests", workspace.resolve(), "Tests"),
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    db = get_db()
    try:
        ensure_corsia_agent(OWNER_USER, model=None, db=db)
        db.commit()
    finally:
        db.close()
    return workspace, _earlier(tmp_path / "earlier")


def _run(source: Path, workspace: Path, *, apply: bool) -> dict:
    from infra.database import get_db
    from onboarding.earlier_install import scan
    from onboarding.earlier_install_apply import run

    db = get_db()
    try:
        return run(scan(source), workspace=workspace, db=db, user_id=OWNER_USER, apply=apply)
    finally:
        db.close()


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git/" not in path.as_posix()
    }


def _states(rows: list[dict], key: str = "path") -> dict[str, str]:
    return {str(row[key]): str(row["state"]) for row in rows}


def _project(report: dict, name: str) -> dict:
    return next(row for row in report["projects"] if row["name"] == name)


def test_the_app_serves_the_import_route():
    """The feature is reachable: without it, this fails on the missing route."""
    from main import app

    paths = set()
    for route in app.routes:
        included = getattr(route, "original_router", None)
        for entry in getattr(included, "routes", []) if included else [route]:
            if path := getattr(entry, "path", None):
                paths.add(path)
    assert "/api/import" in paths


def test_dry_run_writes_nothing(install):
    workspace, earlier = install
    from infra.database import get_db

    before = _tree(workspace)
    report = _run(earlier, workspace, apply=False)

    assert _tree(workspace) == before
    db = get_db()
    try:
        assert db.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 0
    finally:
        db.close()
    assert report["applied"] is False
    assert {row["name"] for row in report["projects"]} == {"Alpha Project", "Beta"}
    alpha = _project(report, "Alpha Project")
    assert alpha["state"] == "new"
    assert _states(alpha["context"])["context/PROJECT.md"] == "new"
    assert _states(alpha["skills"], "name") == {"alpha-skill": "new"}
    assert _states(report["workspace"]["skills"], "name") == {"ws-skill": "new"}


def test_apply_creates_each_project_with_its_content(install):
    workspace, earlier = install
    from core.skill_resolution import local_skill_inventory
    from infra.conductor import get_project_conductor
    from infra.database import get_db
    from registry import service
    from registry.shared_principals import artifact_owner

    report = _run(earlier, workspace, apply=True)

    assert _project(report, "Alpha Project")["state"] == "added"
    assert _project(report, "Beta")["state"] == "added"
    root = workspace / "projects" / "alpha-project"
    assert (root / "context" / "PROJECT.md").read_text() == "# Alpha Project\n\nPurpose.\n"
    assert (root / "skills" / "alpha-skill" / "SKILL.md").is_file()
    assert (root / "shared" / "product" / "facts.md").read_text() == "# Facts\n\nOne fact.\n"
    assert (workspace / "skills" / "ws-skill" / "SKILL.md").is_file()
    # Workspace shared documents land in a workspace that has never held one, so
    # has no shared/ directory of its own yet.
    assert _states(report["workspace"]["shared"])["shared/notes/plan.md"] == "added"
    assert (workspace / "shared" / "notes" / "plan.md").read_text() == "# Plan\n\nThe plan.\n"
    # In a project this run just created, every file there is scaffold seed, so
    # the earlier install's own version of it replaces the seed.
    beta_index = workspace / "projects" / "beta" / "shared" / "INDEX.md"
    assert _states(_project(report, "Beta")["shared"])["shared/INDEX.md"] == "added"
    assert beta_index.read_text() == "# Beta shared\n\nBeta's own blurb.\n"

    db = get_db()
    try:
        row = db.execute("SELECT id FROM projects WHERE name='Alpha Project'").fetchone()
        assert row is not None
        conductor = get_project_conductor(row["id"], db=db)
        assert conductor and db.execute(
            "SELECT project_id FROM threads WHERE id=?", (conductor,),
        ).fetchone()["project_id"] == row["id"]
        # The skill resolver finds it at project scope, so agents there load it.
        names = {skill.name for skill in local_skill_inventory(
            scope="project", workspace=workspace, db=db, project_id=row["id"],
        )}
        assert "alpha-skill" in names
        # A shared document arrives with an identity and an owner, and is indexed.
        artifact = service.find_file_identity(str(root / "shared" / "product" / "facts.md"), db=db)
        assert artifact is not None
        assert artifact_owner(str(artifact["id"]), db=db) == conductor
    finally:
        db.close()
    assert "facts.md" in (root / "shared" / "product" / "INDEX.md").read_text()
    # Context arrived through guidance history, so the save is a commit of its own.
    log = subprocess.run(
        ["git", "log", "--name-only", "--format=%s"],
        cwd=str(workspace), capture_output=True, text=True, timeout=30,
    ).stdout
    assert "projects/alpha-project/context/PROJECT.md" in log


def test_nothing_is_ever_overwritten(install):
    workspace, earlier = install
    from infra.database import get_db

    # A workspace file already at its destination, and a skill of the same name.
    _write(workspace / "shared" / "notes" / "INDEX.md", "# Mine\n\nAlready here.\n")
    _write(workspace / "shared" / "notes" / "plan.md", "# Mine\n\nMy own plan.\n")
    _write(workspace / "skills" / "ws-skill" / "SKILL.md", "# Mine\n")

    first = _run(earlier, workspace, apply=True)
    assert _states(first["workspace"]["shared"])["shared/notes/plan.md"] == "exists"
    assert _states(first["workspace"]["skills"], "name") == {"ws-skill": "exists"}
    assert (workspace / "shared" / "notes" / "plan.md").read_text() == "# Mine\n\nMy own plan.\n"
    assert (workspace / "skills" / "ws-skill" / "SKILL.md").read_text() == "# Mine\n"

    # A second run adds only what is missing: each project is skipped whole.
    before = _tree(workspace)
    second = _run(earlier, workspace, apply=True)
    assert [row["state"] for row in second["projects"]] == ["exists", "exists"]
    assert all(row["reason"] for row in second["projects"])
    assert all(
        row["state"] == "skipped"
        for project in second["projects"]
        for row in project["context"] + project["skills"] + project["shared"]
    )
    assert _tree(workspace) == before
    db = get_db()
    try:
        assert db.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 2
    finally:
        db.close()

    # Something newly missing still comes over on a later run.
    _write(earlier / "skills" / "later-skill" / "SKILL.md", WS_SKILL.replace("ws-skill", "later-skill"))
    third = _run(earlier, workspace, apply=True)
    assert _states(third["workspace"]["skills"], "name")["later-skill"] == "added"
    assert (workspace / "skills" / "later-skill" / "SKILL.md").is_file()


def test_screening_leaves_the_rest_behind(install):
    workspace, earlier = install

    report = _run(earlier, workspace, apply=True)

    arrived = set(_tree(workspace))
    assert not any(part in path for path in arrived for part in (".threads", ".corsia", ".env", "corsia.db"))
    assert not any("source/repo" in path for path in arrived)
    assert not (workspace / "projects" / "alpha-project" / "source" / "repo").exists()
    # Workspace context is not part of this import at all.
    assert not (workspace / "context" / "50-USER-PROFILE.md").exists()

    left = {(row["where"], row["path"]): row["reason"] for row in report["left_behind"]}
    assert left[("workspace", "shared/notes/keys.md")] == "secret"
    assert left[("workspace", "shared/notes/outside.md")] == "link"
    assert not (workspace / "shared" / "notes" / "keys.md").exists()
    assert not (workspace / "shared" / "notes" / "outside.md").exists()
    # The report names the file, never its contents.
    assert "AWS_SECRET_ACCESS_KEY" not in repr(report)
    assert "wJalrXUtnFEMI" not in repr(report)


def test_finder_metadata_never_costs_a_skill(install):
    workspace, earlier = install
    (earlier / "projects" / "alpha" / "skills" / "alpha-skill" / ".DS_Store").write_bytes(b"\x00\x00")
    (earlier / "skills" / ".DS_Store").write_bytes(b"\x00\x00")

    report = _run(earlier, workspace, apply=True)

    assert _states(_project(report, "Alpha Project")["skills"], "name") == {"alpha-skill": "added"}
    assert not any(".DS_Store" in row["path"] for row in report["left_behind"])
    assert not (workspace / "projects" / "alpha-project" / "skills" / "alpha-skill" / ".DS_Store").exists()


def test_dry_run_reads_a_new_projects_skill_as_new(install):
    workspace, earlier = install
    # A workspace skill of that name does not stop a project-scope install.
    _write(workspace / "skills" / "alpha-skill" / "SKILL.md", "# Mine\n")

    dry = _run(earlier, workspace, apply=False)
    assert _states(_project(dry, "Alpha Project")["skills"], "name") == {"alpha-skill": "new"}

    applied = _run(earlier, workspace, apply=True)
    assert _states(_project(applied, "Alpha Project")["skills"], "name") == {"alpha-skill": "added"}


def test_a_folder_inside_a_category_comes_over(install):
    workspace, earlier = install
    _write(earlier / "shared" / "notes" / "2026" / "march" / "deep.md", "# Deep\n\nA dated note.\n")
    _write(earlier / "projects" / "alpha" / "shared" / "product" / "specs" / "one.md", "# One\n\nA spec.\n")

    report = _run(earlier, workspace, apply=True)

    assert _states(report["workspace"]["shared"])["shared/notes/2026/march/deep.md"] == "added"
    assert (workspace / "shared" / "notes" / "2026" / "march" / "deep.md").is_file()
    assert _states(_project(report, "Alpha Project")["shared"])["shared/product/specs/one.md"] == "added"


def test_a_project_folder_that_stays_behind_is_named(install):
    workspace, earlier = install
    _write(earlier / "projects" / "source" / "context" / "PROJECT.md", "# Never\n")
    (earlier / "projects" / "linked").symlink_to(earlier / "projects" / "beta")

    report = _run(earlier, workspace, apply=False)

    assert {row["name"] for row in report["projects"]} == {"Alpha Project", "Beta"}
    left = {row["path"]: row["reason"] for row in report["left_behind"]}
    assert left["projects/source"] == "screened"
    assert left["projects/linked"] == "link"


@pytest.fixture
def client(install):
    """The real route behind the real token check."""
    from api.earlier_install import router
    from infra.database import get_db

    db = get_db()
    try:
        seed_account(OWNER_USER, SERVICE_TOKEN, kind="service", db=db)
        db.commit()
    finally:
        db.close()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as http:
        yield http, install


def _post(http, token: str, source: Path, *, apply: bool = False):
    return http.post(
        "/api/import",
        json={"path": str(source), "apply": apply},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_route_refuses_an_agent_token_and_accepts_the_person(client):
    http, (workspace, earlier) = client

    refused = _post(http, OWNER_TOKEN, earlier)
    assert refused.status_code == 403
    assert _tree(workspace)  # nothing was written, and nothing was removed

    for token in (OWNER_SESSION_TOKEN, SERVICE_TOKEN):
        allowed = _post(http, token, earlier)
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["applied"] is False

    assert _post(http, "not-a-token", earlier).status_code == 401


def test_route_refuses_this_installs_own_workspace(client):
    http, (workspace, _earlier) = client

    answer = _post(http, OWNER_SESSION_TOKEN, workspace)
    assert answer.status_code == 400
    assert "own workspace" in answer.json()["detail"]


def test_route_applies_through_the_running_backend(client):
    http, (workspace, earlier) = client

    answer = _post(http, SERVICE_TOKEN, earlier, apply=True)

    assert answer.status_code == 200, answer.text
    report = answer.json()
    assert report["applied"] is True
    assert _project(report, "Beta")["state"] == "added"
    assert (workspace / "projects" / "beta" / "context" / "PROJECT.md").read_text() == "# Beta\n\nPurpose.\n"
