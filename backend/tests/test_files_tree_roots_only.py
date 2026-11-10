"""The roots-only read: ``GET /api/files/tree?nodes=false``.

A chat link resolves its target against the scope's root paths only, so it asks
for the roots without the file listing. These tests hold the two reads to the
same answer for the roots themselves, and hold the roots-only read to never
walking, never reading the artifact index and never annotating.
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid

import pytest
from auth_fixtures import OWNER_TOKEN

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

pytestmark = pytest.mark.integration

_AUTH = "?token=" + OWNER_TOKEN


def _roots(client, project_id: str | None = None, nodes: bool | None = None) -> list[dict]:
    url = "/api/files/tree" + _AUTH
    if project_id:
        url += f"&project_id={project_id}"
    if nodes is not None:
        url += f"&nodes={str(nodes).lower()}"
    r = client.get(url)
    assert r.status_code == 200, r.text
    return r.json()["roots"]


@pytest.fixture
def two_repo_project(integration_workspace):
    """A project with a planning folder and two registered repos, each with a file."""
    from infra import config
    from infra.database import get_db
    from infra.workspace_paths import WorkspaceReference

    name = f"Roots {uuid.uuid4().hex[:6]}"
    slug = name.lower().replace(" ", "-")
    proj_dir = integration_workspace / "projects" / slug
    source_dir = proj_dir / "source"
    source_dir.mkdir(parents=True)
    (proj_dir / "shared").mkdir()
    (proj_dir / "shared" / "brief.md").write_text("# Brief\n")

    repos = {}
    for repo_name in ("alpha", "beta"):
        repo_dir = integration_workspace / f"roots-repo-{repo_name}-{uuid.uuid4().hex[:6]}"
        repo_dir.mkdir()
        (repo_dir / "README.md").write_text("# repo\n")
        (source_dir / repo_name).symlink_to(repo_dir, target_is_directory=True)
        repos[repo_name] = repo_dir

    pid = uuid.uuid4().hex
    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id, name, path, created_at, updated_at) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
            (
                pid,
                name,
                WorkspaceReference.from_path(
                    proj_dir, config.WORKSPACE_SELECTION
                ).serialize(),
            ),
        )
        for repo_name, repo_dir in repos.items():
            db.execute(
                "INSERT INTO project_repo_policies "
                "(project_id, repo_name, repo_path, pinned_branch, protect_base_checkout) "
                "VALUES (?, ?, ?, NULL, 0)",
                (pid, repo_name, str(repo_dir.resolve())),
            )
        db.commit()
    finally:
        db.close()

    yield {"project_id": pid, "planning": proj_dir, "repos": repos}

    db = get_db()
    try:
        db.execute("DELETE FROM project_repo_policies WHERE project_id=?", (pid,))
        db.execute("DELETE FROM projects WHERE id=?", (pid,))
        db.commit()
    finally:
        db.close()
    shutil.rmtree(proj_dir, ignore_errors=True)
    for repo_dir in repos.values():
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_roots_only_answers_the_same_roots_as_the_tree_read(integration_client):
    """Same entries, same order, minus the listing."""
    full = _roots(integration_client)
    roots_only = _roots(integration_client, nodes=False)

    assert roots_only == [{k: v for k, v in root.items() if k not in ("nodes", "capped")} for root in full]
    assert roots_only and roots_only[0]["kind"] == "workspace"


def test_roots_only_answers_the_same_roots_for_a_project(integration_client, two_repo_project):
    pid = two_repo_project["project_id"]
    full = _roots(integration_client, pid)
    roots_only = _roots(integration_client, pid, nodes=False)

    assert roots_only == [{k: v for k, v in root.items() if k not in ("nodes", "capped")} for root in full]
    assert [(root["id"], root["kind"]) for root in roots_only] == [
        ("planning", "workspace"),
        ("repo:alpha", "repo"),
        ("repo:beta", "repo"),
    ]


def test_roots_only_carries_no_listing(integration_client, two_repo_project):
    """The answer is roots alone: no nodes, no capped flag, and no file names."""
    roots_only = _roots(integration_client, two_repo_project["project_id"], nodes=False)

    assert roots_only
    for root in roots_only:
        assert "nodes" not in root
        assert "capped" not in root
    assert "README.md" not in str(roots_only)


def test_roots_only_never_walks_or_reads_the_artifact_index(
    integration_client, two_repo_project, monkeypatch
):
    """The walk, the artifact index and the annotation belong to the tree read alone."""
    import api.files as files_api

    calls = {"walk": 0, "artifact_index": 0, "shared_projector": 0}
    for name in calls:
        def counted(*args, name=name, real=getattr(files_api, name)):
            calls[name] += 1
            return real(*args)
        monkeypatch.setattr(files_api, name, counted)

    pid = two_repo_project["project_id"]
    _roots(integration_client, pid, nodes=False)
    assert calls == {"walk": 0, "artifact_index": 0, "shared_projector": 0}

    _roots(integration_client, pid)
    assert calls == {"walk": 3, "artifact_index": 1, "shared_projector": 1}


def test_roots_only_keeps_the_same_auth_and_404(integration_client):
    """A bad token and an unknown project are refused exactly as the tree read refuses them."""
    for query in ("token=not-a-token&", "token=&"):
        assert integration_client.get("/api/files/tree?" + query + "nodes=false").status_code == 401

    unknown = "&project_id=no-such-project"
    full = integration_client.get("/api/files/tree" + _AUTH + unknown)
    roots_only = integration_client.get("/api/files/tree" + _AUTH + unknown + "&nodes=false")
    assert (full.status_code, full.json()) == (roots_only.status_code, roots_only.json())
    assert roots_only.status_code == 404
    assert roots_only.json()["detail"] == "Project not found"
