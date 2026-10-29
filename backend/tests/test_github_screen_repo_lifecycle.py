"""Screen installation follows the real repository lifecycle without manual sync."""
import asyncio
import json
import subprocess
from pathlib import Path

from api import project_repos
from events import observe_rules
from events.connectors.github_screen import install
from infra import project_repo_policy
from infra.conductor import set_project_conductor
from infra.database import get_db
from infra.measure_registry import REGISTRY
from tests.event_routing_helpers import mk_session


def test_real_repository_api_changes_reconcile_automatically(isolated_db, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "screen"
    project.mkdir(parents=True)
    repo = tmp_path / "source-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/Example/Repo.git"],
                   check=True, capture_output=True)
    mk_session("api-owner", project_id="api-project")
    db = get_db()
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('api-project','Screen','t','t')")
    db.commit()
    REGISTRY.reset()
    monkeypatch.setattr(project_repo_policy, "WORKSPACE", workspace)
    monkeypatch.setattr(install, "owner_root", lambda owner, project_id, conn: project / ".threads" / owner)
    monkeypatch.setattr(project_repos, "_workspace_dir_for", lambda _: project)
    monkeypatch.setattr(project_repos, "get_db", lambda: db)
    monkeypatch.setattr(project_repos, "authenticate", lambda _: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    async def run_now(callback):
        return callback()
    monkeypatch.setattr(project_repos, "run_in_db", run_now)
    assert set_project_conductor("api-project", "api-owner", db=db)
    observe_rules.sync_shipped_rules(db)
    key = install._key("api-project")
    assert db.execute("SELECT id FROM automation_schedules WHERE id=?", (key + "_script",)).fetchone() is None
    added = asyncio.run(project_repos.add_repo("token", "api-project", project_repos.RepoAdd(
        mode="link", path=str(repo), pinned_branch=None, protect_base_checkout=False)))
    assert added["repo"]["registered"]
    assert db.execute("SELECT id FROM automation_schedules WHERE id=?", (key + "_script",)).fetchone()
    rows = db.execute("SELECT action, \"match\" FROM event_rules WHERE scope=? AND origin=?",
                      ("project:api-project", install.origin())).fetchall()
    assert len(rows) == 3
    assert all(json.loads(row["match"])[0] == {"field": "repo", "op": "in", "value": ["example/repo"]}
               for row in rows)
    assert db.execute("SELECT id FROM automation_schedules WHERE id=?", (key + "_script",)).fetchone()
    asyncio.run(project_repos.detach_repo("token", "api-project", "source-repo"))
    assert not db.execute("SELECT id FROM event_rules WHERE scope=? AND origin=?",
                          ("project:api-project", install.origin())).fetchall()
    assert db.execute("SELECT id FROM automation_schedules WHERE id=?", (key + "_script",)).fetchone() is None
