"""Every Measurement section answers only for its selected scope: another project's rows never appear."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_measurement_api import _seed_outcomes, _seed_thread

USER = {"id": "steve"}
# Wednesday of the last completed period: inside every Trend window by construction.
SECTIONS = {
    "series": "/api/measurement/series?ids=outcomes,fresh_tokens,limit_hits&grain=weeks&timezone=UTC",
    "series_index": "/api/measurement/series/index?",
    "agents": "/api/measurement/agents?grain=weeks&timezone=UTC",
    "models": "/api/measurement/models?grain=weeks&timezone=UTC",
    "cost": "/api/measurement/cost?grain=weeks&timezone=UTC",
    "limits": "/api/measurement/limits?grain=weeks&timezone=UTC",
    "key_figures": "/api/measurement/key-figures?grain=weeks&timezone=UTC",
    "lessons": "/api/measurement/lessons?range=26w&preview=true",
}


def _seed() -> None:
    from infra.database import get_db
    from infra.time import utc_now

    clock = utc_now()
    day = clock.date() - timedelta(days=clock.date().weekday() + 5)
    stamp = f"{day.isoformat()}T12:00:00Z"
    db = get_db()
    try:
        for project_id, name in (("alpha", "Alpha Project"), ("beta", "Beta Project")):
            db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,'t','t')", (project_id, name))
        _seed_thread(db, "agent-alpha", project_id="alpha", agent=True)
        _seed_thread(db, "peer-alpha", project_id="alpha", agent=True)
        _seed_thread(db, "agent-beta", project_id="beta", agent=True)
        # A worker thread carries no project of its own; it belongs to its root's project.
        _seed_thread(db, "worker-alpha", parent_id="agent-alpha")
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,total_tokens,cost_usd,source,created_at) "
            "VALUES ('u-worker','worker-alpha','model-alpha',10,5,15,1.0,'turn',?)", (stamp,),
        )
        for agent in ("agent-alpha", "peer-alpha", "agent-beta"):
            _seed_outcomes(db, agent, count=3)
            model = "model-beta" if agent == "agent-beta" else "model-alpha"
            db.execute(
                "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,total_tokens,cost_usd,source,created_at) "
                "VALUES (?,?,?,10,5,15,1.0,'turn',?)", (f"u-{agent}", agent, model, stamp),
            )
            db.execute(
                "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,ended_at,error,boot_epoch) "
                "VALUES (?,?,0,?,'error',?,?,'usage limit','test')", (f"t-{agent}", agent, model, stamp, stamp),
            )
        db.commit()
    finally:
        db.close()


def _client() -> TestClient:
    from api import measurement

    app = FastAPI()
    app.include_router(measurement.router)
    app.dependency_overrides[measurement.require_user] = lambda: USER
    return TestClient(app)


def _get(client: TestClient, path: str, project: str, agent: str) -> dict:
    separator = "" if path.endswith("?") else "&"
    response = client.get(f"{path}{separator}project={project}&agent={agent}")
    assert response.status_code in (200, 204), (path, response.status_code, response.text)
    return response.json() if response.status_code == 200 else {}


@pytest.fixture
def seeded(isolated_db):
    _seed()
    return _client()


@pytest.mark.parametrize("section", sorted(SECTIONS))
@pytest.mark.parametrize(("project", "agent", "peer_visible"), [("alpha", "none", True), ("all", "agent-alpha", False)])
def test_selected_scope_never_carries_another_projects_rows(seeded, section, project, agent, peer_visible):
    body = json.dumps(_get(seeded, SECTIONS[section], project, agent)).lower()
    assert "beta" not in body
    if not peer_visible:
        assert "peer-alpha" not in body


def test_workspace_wide_facts_are_served_only_at_all_work(seeded):
    scopes = {"all": ("all", "none"), "project": ("alpha", "none"), "agent": ("all", "agent-alpha")}
    cost = {name: _get(seeded, SECTIONS["cost"], *scope)["actual"] for name, scope in scopes.items()}
    limits = {name: _get(seeded, SECTIONS["limits"], *scope) for name, scope in scopes.items()}
    assert cost["all"]["workspace_subscription"] is not None
    assert limits["all"]["accounts"] is not None
    for name in ("project", "agent"):
        assert cost[name]["workspace_subscription"] is None
        assert limits[name]["accounts"] is None


def test_the_workspace_rule_lives_once_on_the_scope():
    from core.measurement_scope import MeasurementScope, is_workspace_scope

    assert is_workspace_scope("all", None) and MeasurementScope("all", None).is_workspace
    for project, agent in ((("alpha"), None), ("all", "agent-alpha"), ("alpha", "agent-alpha")):
        assert not is_workspace_scope(project, agent)
        assert not MeasurementScope(project, agent).is_workspace


@pytest.mark.parametrize("section", ["cost", "limits", "models", "agents", "key_figures"])
def test_a_project_with_no_threads_yet_is_an_empty_scope_not_an_error(seeded, section):
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('fresh','Fresh Project','t','t')")
        db.commit()
    finally:
        db.close()
    path = SECTIONS[section]
    response = seeded.get(f"{path}{'' if path.endswith('?') else '&'}project=fresh&agent=none")
    assert response.status_code in (200, 204), (section, response.status_code, response.text)
    assert "alpha" not in response.text.lower() and "beta" not in response.text.lower()
    unknown = seeded.get(f"{path}{'' if path.endswith('?') else '&'}project=absent&agent=none")
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_spend_summary_is_scoped_in_every_breakdown(isolated_db, monkeypatch):
    from api import costs

    _seed()
    monkeypatch.setattr(costs, "authenticate", lambda _: USER)
    summary = await costs.cost_summary("t", project="alpha", agent="none", range="26w")
    assert "beta" not in json.dumps({key: summary[key] for key in ("by_model", "by_project", "by_source", "top_threads", "weekly", "totals")}).lower()
