"""The agent KPI HTTP read computes declared formulas from stored facts."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.measurement_agent_kpis import router
from infra.auth import require_user
from infra.database import get_db
from infra.measure_registry import WorkflowOwner
from tests.test_measurement_api import _seed_thread


@pytest.fixture
def client(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr("core.measurement_scope._now", lambda: datetime(2026, 1, 20, tzinfo=timezone.utc))
    monkeypatch.setattr("api.measurement_agent_kpis._repositories", lambda db, project: {"org/repo"})
    monkeypatch.setattr("core.agent_kpi_read.thread_workflow_owners",
                        lambda owner, db: {"demo": WorkflowOwner(tmp_path, "owner")})
    db = get_db()
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('p','P','t','t')")
    _seed_thread(db, "owner", project_id="p", agent=True)
    db.commit()
    db.close()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {"id": "steve"}
    with TestClient(app) as result:
        yield result, tmp_path


def definition(db, tmp_path: Path, kind, name, spec):
    db.execute("""INSERT INTO measure_definitions(owner_thread_id,kind,name,version,spec_json,file_path,file_sha256,synced_at)
        VALUES ('owner',?,?,1,?,?, 'hash','2026-01-01')""", (kind, name, json.dumps(spec), str(tmp_path / "measures.yaml")))


def fact(db, name, day, attrs, *, source="agent", agent="owner", project="p", subject=None):
    identity = f"{name}:{day}:{attrs}"
    db.execute("""INSERT INTO outcome_events(id,dedupe_key,event_name,source,provenance,ts,observed_ts,
        subject_key,project_id,agent_thread_id,attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (identity, identity, name, source, "observed" if source == "github" else "agent_recorded",
         f"2026-01-{day:02d}T12:00:00+00:00", "2026-01-20T00:00:00+00:00", subject, project, agent, json.dumps(attrs)))


def read(client):
    response = client.get("/agent-kpis", params={"project": "p", "agent": "owner", "timezone": "UTC", "grain": "weeks"})
    assert response.status_code == 200, response.text
    return response.json()


def item(result, name):
    return next(row for row in result["kpis"] if row["name"] == name)


def period(result, name, date="2026-01-05"):
    return next(row for row in item(result, name)["periods"] if row["period"] == date)


@pytest.mark.parametrize("formula,operand,extra,expected", [
    ("count", {"event": "demo.done"}, {}, 3),
    ("ratio", {"event": "demo.done", "where": {"kind": "yes"}}, {"den": {"event": "demo.done"}}, 2 / 3),
    ("percentile", {"event": "demo.done", "attr": "n", "p": 50}, {}, 2),
    ("share", {"event": "demo.done", "attr": "kind", "values": ["yes"]}, {}, 2 / 3),
])
def test_formulas_through_http(client, formula, operand, extra, expected):
    http, path = client
    db = get_db()
    definition(db, path, "event", "demo.done", {"attributes": {}})
    definition(db, path, "kpi", formula, {"title": formula, "formula": formula, "num": operand, "min_n": 1, **extra})
    for index, day in enumerate((5, 6, 7), 1):
        fact(db, "demo.done", day, {"n": index, "kind": "yes" if index < 3 else "no", "workflow_version": "abc"})
    db.commit()
    db.close()
    result = period(read(http), formula)
    assert result["value"] == expected
    assert result["label"] == "Self-reported"
    assert result["n"] == 3


def test_descendant_facts_respect_user_scope(client):
    http, path = client
    db = get_db()
    definition(db, path, "event", "demo.done", {"attributes": {}})
    definition(db, path, "kpi", "items", {"formula": "count", "num": {"event": "demo.done"}, "min_n": 1})
    _seed_thread(db, "worker", project_id="p", parent_id="owner")
    _seed_thread(db, "foreign", user_id="other", project_id="p", parent_id="owner")
    fact(db, "demo.done", 5, {"key": "allowed"}, agent="worker")
    fact(db, "demo.done", 6, {"key": "excluded"}, agent="foreign")
    db.commit()
    db.close()
    assert period(read(http), "items")["value"] == 1


def test_late_label_anchors_to_run_and_version(client):
    http, path = client
    db = get_db()
    definition(db, path, "event", "demo.run", {"attributes": {}})
    definition(db, path, "event", "demo.label", {"attributes": {}})
    spec = {"formula": "count", "num": {"event": "demo.label", "latest": ["run_id"],
            "match": [{"event": "demo.run", "on": "run_id", "mode": "exists", "anchor": True}]}, "min_n": 1}
    definition(db, path, "kpi", "labels", spec)
    fact(db, "demo.run", 6, {"run_id": "a", "workflow_version": "hash"})
    fact(db, "demo.label", 18, {"run_id": "a", "label": "new"})
    fact(db, "demo.label", 15, {"run_id": "a", "label": "old"})
    db.commit()
    db.close()
    result = read(http)
    assert period(result, "labels")["value"] == 1
    assert next(v for v in item(result, "labels")["versions"] if v["version"] == "hash")["label"] == "v1"
    assert next(v for v in period(result, "labels")["versions"] if v["version"] == "hash")["value"] == 1


def test_coverage_weakest_label_and_too_few(client):
    http, path = client
    db = get_db()
    definition(db, path, "event", "demo.review", {"attributes": {}})
    definition(db, path, "event", "demo.label", {"attributes": {}})
    definition(db, path, "kpi", "labels", {"formula": "count", "num": {"event": "demo.label"},
        "coverage": {"of": {"event": "demo.review"}, "by": "run_id"}, "min_n": 2})
    fact(db, "demo.review", 6, {"run_id": "a", "workflow_version": "hash"})
    fact(db, "demo.label", 7, {"run_id": "a", "workflow_version": "hash"})
    db.commit()
    db.close()
    row = period(read(http), "labels")
    assert row["value"] is None and row["status"] == "too few"
    assert row["coverage"] == {"n": 1, "k": 1} and row["n"] == 1
    assert row["label"] == "Self-reported"


def test_synced_version_attribute_and_missing_metadata(client):
    http, path = client
    db = get_db()
    definition(db, path, "event", "demo.done", {"attributes": {}})
    definition(db, path, "kpi", "items", {"formula": "count", "num": {"event": "demo.done"}, "min_n": 1})
    fact(db, "demo.done", 6, {"revision": "rev1"})
    db.commit()
    db.close()
    assert any(v["label"] == "Unversioned" for v in item(read(http), "items")["versions"])
    db = get_db()
    db.execute("INSERT INTO measure_files VALUES ('owner',?,'hash','revision','2026-01-01')", (str(path / "measures.yaml"),))
    db.commit()
    db.close()
    assert any(v["version"] == "rev1" and v["label"] == "v1" for v in item(read(http), "items")["versions"])


def test_first_review_and_push_with_no_suite(client):
    http, path = client
    db = get_db()
    definition(db, path, "kpi", "first", {"formula": "count", "num": {"event": "github.review", "first": ["repo", "pr_number"]}, "min_n": 1})
    definition(db, path, "kpi", "triggered", {"formula": "count", "num": {"event": "github.push", "match": [{"event": "github.suite", "on": "head_sha", "mode": "exists"}]}, "min_n": 1})
    fact(db, "github.review", 7, {"repo": "org/repo", "pr_number": "1"}, source="github", agent=None, project=None)
    fact(db, "github.review", 5, {"repo": "org/repo", "pr_number": "1"}, source="github", agent=None, project=None)
    fact(db, "github.push", 5, {"repo": "org/repo", "head_sha": "a"}, source="github", agent=None, project=None)
    fact(db, "github.push", 6, {"repo": "org/repo", "head_sha": "b"}, source="github", agent=None, project=None)
    fact(db, "github.suite", 7, {"repo": "org/repo", "head_sha": "b"}, source="github", agent=None, project=None)
    db.commit()
    db.close()
    result = read(http)
    assert period(result, "first")["value"] == 1
    assert period(result, "triggered")["value"] == 1
