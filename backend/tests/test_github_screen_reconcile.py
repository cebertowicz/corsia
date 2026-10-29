"""Boot and after-commit reconciliation use live repository and package state."""
import json
from dataclasses import replace

import pytest

from events import connectors
from events.connectors.github_notifications_feed import FeedError
from events.connectors.github_screen import install
from infra.conductor import set_project_conductor
from infra.database import get_db, write_transaction
from tests.event_routing_helpers import mk_session


@pytest.fixture
def project(isolated_db, tmp_path, monkeypatch):
    mk_session("screen-owner", project_id="screen-project")
    from infra.schema import SCHEMA_VERSION

    db = get_db()
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    db.execute("INSERT INTO projects (id,name,created_at,updated_at,conductor_thread_id) "
               "VALUES ('screen-project','Screen','t','t','screen-owner')")
    db.commit()
    reads = []

    def repositories(project_id):
        assert not db.in_transaction
        reads.append(project_id)
        return frozenset({"o/r"})

    monkeypatch.setattr(install, "tracked_github_repositories", repositories)
    monkeypatch.setattr(install, "owner_root", lambda *_args: tmp_path / "owner")
    monkeypatch.setattr("automation.worker_inputs.owner_root", lambda *_args: tmp_path / "owner")
    return db, reads, tmp_path / "owner" / "github-screen" / install._key("screen-project")


def _installed(db):
    return [dict(row) for row in db.execute("SELECT * FROM event_rules WHERE scope='project:screen-project' ORDER BY id")]


def test_boot_installs_existing_projects_and_rebinds_package_updates(project, tmp_path, monkeypatch):
    from infra.database import init_db

    db, reads, folder = project
    # The fixture seeds catalog rows; provider discovery is unrelated to this boot reconcile.
    monkeypatch.setattr("connections.seed_builtin_connections", lambda: None)
    assert not _installed(db)
    init_db()
    assert len(_installed(db)) == 3 and reads == ["screen-project"]
    script = folder / "stage.py"
    assert script.read_bytes() == (install.PACKAGE / "stage.py").read_bytes()
    before = db.execute("SELECT script_sha256 FROM automation_schedules WHERE id=?",
                        (install._key("screen-project") + "_script",)).fetchone()[0]
    package = tmp_path / "updated-package"
    package.mkdir()
    for name in ("stage.py", "floor.py"):
        (package / name).write_bytes((install.PACKAGE / name).read_bytes() + b"\n# updated package\n")
    (folder / "config.json").write_text('{"repositories":["stale/repo"]}')
    monkeypatch.setattr(install, "PACKAGE", package)
    manifest = connectors.get_manifest("github")
    monkeypatch.setitem(connectors._REGISTRY, "github", replace(manifest, version="upgraded"))
    init_db()
    assert all(row["origin"] == "rule_set:github@upgraded" for row in _installed(db))
    assert script.read_bytes() == (package / "stage.py").read_bytes()
    assert (folder / "floor.py").read_bytes() == (package / "floor.py").read_bytes()
    schedule = db.execute("SELECT * FROM automation_schedules WHERE id=?",
                          (install._key("screen-project") + "_script",)).fetchone()
    assert schedule["script_sha256"] == install.file_sha256(script) != before
    assert not (folder / "config.json").exists()
    changes, mtime = db.total_changes, script.stat().st_mtime_ns
    install.sync(db)
    assert db.total_changes == changes and script.stat().st_mtime_ns == mtime


def test_feed_failure_preserves_project_rows_and_does_not_abort_designation(project, monkeypatch, caplog):
    db, _, _ = project
    install.sync(db)
    rows = _installed(db)
    schedule = dict(db.execute("SELECT * FROM automation_schedules WHERE thread_id='screen-owner'").fetchone())
    mk_session("new-owner", project_id="screen-project")

    def unavailable(_project):
        assert not db.in_transaction
        raise FeedError("repo_scope_unavailable")

    monkeypatch.setattr(install, "tracked_github_repositories", unavailable)
    assert set_project_conductor("screen-project", "new-owner", db=db)
    assert _installed(db) == rows
    assert dict(db.execute("SELECT * FROM automation_schedules WHERE id=?", (schedule["id"],)).fetchone()) == schedule
    assert len([r for r in caplog.records if "scope unavailable" in r.message]) == 1
    assert db.execute("SELECT conductor_thread_id FROM projects WHERE id='screen-project'").fetchone()[0] == "new-owner"


def test_designation_reconciles_only_after_outer_commit_and_never_on_rollback(project):
    db, reads, _ = project
    with write_transaction(db, scope="designation_outer"):
        assert set_project_conductor("screen-project", None, db=db)
        assert reads == [] and not _installed(db)
    assert not _installed(db)
    with write_transaction(db, scope="designation_outer"):
        assert set_project_conductor("screen-project", "screen-owner", db=db)
        assert reads == [] and not _installed(db)
    assert reads == ["screen-project"] and len(_installed(db)) == 3
    with pytest.raises(RuntimeError):
        with write_transaction(db, scope="designation_rollback"):
            set_project_conductor("screen-project", None, db=db)
            raise RuntimeError("rollback")
    assert reads == ["screen-project"] and len(_installed(db)) == 3


def test_repo_sync_waits_for_commit_and_drops_rollback(project, monkeypatch, tmp_path):
    from api import project_repo_sync

    db, reads, _ = project
    monkeypatch.setattr(project_repo_sync, "compute_doc_roots", lambda _path: [])
    monkeypatch.setattr(project_repo_sync, "_write_doc_roots", lambda *_args: None)
    with write_transaction(db, scope="repos_outer"):
        project_repo_sync.sync_sources("screen-project", tmp_path, db)
        assert reads == []
    assert reads == ["screen-project"] and len(_installed(db)) == 3
    with pytest.raises(RuntimeError):
        with write_transaction(db, scope="repos_rollback"):
            project_repo_sync.sync_sources("screen-project", tmp_path, db)
            raise RuntimeError("rollback")
    assert reads == ["screen-project"]


def test_execution_input_uses_live_scope_not_installed_repo_match(project, monkeypatch):
    from automation.script_rule_runs import RuleRun, execution_input
    from automation.schedules import get
    from automation.script_sandbox import ScriptSandboxError

    db, _, _ = project
    install.sync(db)
    schedule = get(install._key("screen-project") + "_script", db=db)
    manifest = connectors.get_manifest("github")
    repos = {"o/r"}
    monkeypatch.setitem(connectors._REGISTRY, "github", replace(manifest, project_repositories=lambda _: repos))
    run = RuleRun(1, "rule", json.dumps({"facts": {"repo": "o/r"}}).encode(), (), "o/r")
    event = json.loads(execution_input(schedule, run))
    assert event["context"] == {"project_id": "screen-project", "staging_root": str(project[2].parents[1] / "staging"),
                                "event_type": install._namespace("screen-project") + ".pr_staged", "github_repo": "o/r"}
    repos.clear()
    with pytest.raises(ScriptSandboxError, match="no longer registered"):
        execution_input(schedule, run)
    assert json.loads(_installed(db)[0]["match"])[0]["value"] == ["o/r"]
    monkeypatch.setitem(connectors._REGISTRY, "github", replace(manifest, project_repositories=lambda _: None))
    with pytest.raises(ScriptSandboxError, match="Couldn’t read"):
        execution_input(schedule, run)
