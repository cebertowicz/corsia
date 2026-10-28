"""Lifecycle and matching for the shipped outside-PR screen."""
import json
from dataclasses import replace

import pytest

from events import connectors, observe_rules, rule_declarations, rules
from events.connectors.github_screen import install
from infra.conductor import set_project_conductor
from infra.database import get_db, write_transaction
from infra.measure_registry import REGISTRY
from tests.event_routing_helpers import mk_session


def _rows(db, table, where, args):
    return [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY id", args)]


def test_screen_reconciles_full_set_and_conductor_changes(isolated_db, tmp_path, monkeypatch):
    mk_session("screen-owner", project_id="screen-project")
    mk_session("screen-new-owner", project_id="screen-project")
    db = get_db()
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('screen-project','Screen','t','t')")
    db.commit()
    REGISTRY.reset()
    repos = []
    monkeypatch.setattr(install, "_repos", lambda conn, project: list(repos))
    monkeypatch.setattr(install, "owner_root", lambda owner, project, conn: tmp_path / owner)
    assert set_project_conductor("screen-project", "screen-owner", db=db)
    observe_rules.sync_shipped_rules(db, "screen-project")
    assert not _rows(db, "event_rules", "origin=? AND scope=?", (install.origin(), "project:screen-project"))

    repos.extend(["example/one", "example/two"])
    observe_rules.sync_shipped_rules(db, "screen-project")
    scope = "project:screen-project"
    rows = _rows(db, "event_rules", "origin=? AND scope=?", (install.origin(), scope))
    schedules = _rows(db, "automation_schedules", "id=?", (install._key("screen-project") + "_script",))
    assert len(rows) == 3 and len(schedules) == 1
    assert {r["action"] for r in rows} == {"run_script", "start_worker"}
    assert all(r["created_by"] == "screen-owner" and r["to"] == "thread:screen-owner" for r in rows)
    for row in rows:
        assert json.loads(row["match"])[0] == {"field": "repo", "op": "in", "value": repos}
        if row["action"] == "start_worker":
            assert rules.matches(rules.from_row(row), {"repo": "example/one"})
            assert not rules.matches(rules.from_row(row), {"repo": "example/other"})
        if row["action"] == "run_script":
            rule = rules.from_row(row)
            assert rules.matches(rule, {"repo": "EXAMPLE/ONE", "author_origin": "outside"})
            assert not rules.matches(rule, {"repo": "example/one", "author_origin": "inside"})
            assert not rules.matches(rule, {"repo": "example/other", "author_origin": "outside"})
    namespace = install._namespace("screen-project")
    assert rule_declarations.declared_event(db, namespace, "pr_staged")[1].owner == "screen-owner"
    config = json.loads((tmp_path / "screen-owner" / "github-screen" / install._key("screen-project") / "config.json").read_text())
    assert config["repositories"] == repos
    observe_rules.sync_shipped_rules(db, "screen-project")
    assert rows == _rows(db, "event_rules", "origin=? AND scope=?", (install.origin(), scope))
    assert schedules == _rows(db, "automation_schedules", "id=?", (install._key("screen-project") + "_script",))

    assert set_project_conductor("screen-project", "screen-new-owner", db=db)
    rows = _rows(db, "event_rules", "origin=? AND scope=?", (install.origin(), scope))
    assert len(rows) == 3 and all(r["created_by"] == "screen-new-owner" and r["to"] == "thread:screen-new-owner" for r in rows)
    schedule = _rows(db, "automation_schedules", "id=?", (install._key("screen-project") + "_script",))[0]
    assert schedule["thread_id"] == schedule["created_by_actor"] == "screen-new-owner"
    assert rule_declarations.declared_event(db, namespace, "pr_staged")[1].owner == "screen-new-owner"
    manifest = connectors.get_manifest("github")
    monkeypatch.setitem(connectors._REGISTRY, "github", replace(manifest, version="2"))
    observe_rules.sync_shipped_rules(db, "screen-project")
    assert len(_rows(db, "event_rules", "origin=? AND scope=?", ("rule_set:github@2", scope))) == 3
    with pytest.raises(RuntimeError):
        with write_transaction(db, scope="test_screen_rollback"):
            assert set_project_conductor("screen-project", None, db=db)
            raise RuntimeError("roll back designation")
    assert rule_declarations.declared_event(db, namespace, "pr_staged")[1].owner == "screen-new-owner"
    assert len(_rows(db, "event_rules", "origin=? AND scope=?", (install.origin(), scope))) == 3
    repos.clear()
    observe_rules.sync_shipped_rules(db, "screen-project")
    assert not _rows(db, "event_rules", "origin=? AND scope=?", (install.origin(), scope))
    assert not _rows(db, "automation_schedules", "id=?", (install._key("screen-project") + "_script",))
    assert rule_declarations.declared_event(db, namespace, "pr_staged") is None
