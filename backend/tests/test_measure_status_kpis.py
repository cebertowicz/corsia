"""measure_status returns the calling agent's own KPI values when asked, through execute()."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from api import measurement_agent_kpis as agent_kpis
from infra.database import get_db
from infra.measure_registry import WorkflowOwner
from tests.test_agent_kpi_api import definition, fact
from tests.test_agent_kpi_builtin_api import task
from tests.test_measurement_api import _seed_thread
from tools.measures import MeasureStatusTool


@pytest.fixture
def owner(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr("core.measurement_scope._now", lambda: datetime(2026, 1, 20, tzinfo=timezone.utc))
    monkeypatch.setattr("api.measurement_agent_kpis._repositories", lambda db, project: set())
    monkeypatch.setattr("core.agent_kpi_read.thread_workflow_owners",
                        lambda thread, db: {"demo": WorkflowOwner(tmp_path, "owner")})
    db = get_db()
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('p','P','t','t')")
    _seed_thread(db, "owner", project_id="p", agent=True)
    db.commit()
    db.close()
    return tmp_path


def seed_kpis(path) -> None:
    db = get_db()
    definition(db, path, "event", "demo.done", {"attributes": {}})
    definition(db, path, "kpi", "items", {"title": "Items", "formula": "count", "num": {"event": "demo.done"}, "min_n": 1})
    definition(db, path, "kpi", "late", {"title": "Late", "formula": "ratio", "num": {"event": "demo.done", "where": {"kind": "late"}},
                                         "den": {"event": "demo.done"}, "min_n": 1})
    for day, kind in ((13, "late"), (14, "ok"), (20, "ok")):
        fact(db, "demo.done", day, {"kind": kind})
    db.commit()
    db.close()


@pytest.mark.asyncio
async def test_kpis_true_returns_each_synced_kpi_current_and_last_complete_week(owner) -> None:
    seed_kpis(owner)
    shown = json.loads(await MeasureStatusTool().execute(_thread_id="owner", kpis=True))
    assert {"events", "synced_files"} <= shown.keys()
    items, late = sorted(shown["kpis"], key=lambda row: row["name"])
    assert set(items) == {"name", "definition_version", "workflow", "current", "last_complete"}
    assert items["name"] == "items" and late["name"] == "late"
    assert items["last_complete"] == {"period": "2026-01-12", "value": 2, "n": 2, "unknown": 0, "status": "ok",
                                       "partial": False}
    assert {key: value for key, value in items["current"].items() if key != "partial"} == {
        "period": "2026-01-19", "value": 1, "n": 1, "unknown": 0, "status": "ok"}
    assert isinstance(items["current"]["partial"], bool)
    assert late["last_complete"]["value"] == 0.5 and late["current"]["value"] == 0


@pytest.mark.asyncio
async def test_kpis_read_only_the_two_periods_they_return(owner, monkeypatch) -> None:
    seed_kpis(owner)
    starts = set()
    real = agent_kpis.evaluate

    def spy(spec, events, start, *args, **kwargs):
        starts.add(start.date().isoformat())
        return real(spec, events, start, *args, **kwargs)

    monkeypatch.setattr(agent_kpis, "evaluate", spy)
    await MeasureStatusTool().execute(_thread_id="owner", kpis=True)
    assert starts == {"2026-01-12", "2026-01-19"}


@pytest.mark.asyncio
async def test_default_call_has_no_kpis_key(owner) -> None:
    seed_kpis(owner)
    assert "kpis" not in json.loads(await MeasureStatusTool().execute(_thread_id="owner"))


@pytest.mark.asyncio
async def test_agent_with_no_kpis_gets_an_empty_list(owner) -> None:
    assert json.loads(await MeasureStatusTool().execute(_thread_id="owner", kpis=True))["kpis"] == []


@pytest.mark.asyncio
async def test_an_agent_outside_any_project_reads_its_kpis(owner) -> None:
    db = get_db()
    _seed_thread(db, "workspace-agent", agent=True)
    db.commit()
    db.close()
    assert json.loads(await MeasureStatusTool().execute(_thread_id="workspace-agent", kpis=True))["kpis"] == []


@pytest.mark.asyncio
async def test_a_plain_thread_reads_its_own_kpis_with_its_child_threads_events_and_workflow_builtins(owner, monkeypatch) -> None:
    monkeypatch.setattr("core.agent_kpi_read.thread_workflow_owners",
                        lambda thread, db: {"demo": WorkflowOwner(owner, "plain")})
    db = get_db()
    _seed_thread(db, "plain", project_id="p")
    _seed_thread(db, "child", project_id="p", parent_id="plain")
    definition(db, owner, "event", "demo.done", {"attributes": {}}, owner="plain")
    definition(db, owner, "kpi", "items", {"title": "Items", "formula": "count", "num": {"event": "demo.done"}, "min_n": 1},
               owner="plain")
    definition(db, owner, "kpi", "runs", {"title": "Runs", "formula": "count", "num": {"builtin": "runs"}, "min_n": 1},
               owner="plain")
    fact(db, "demo.done", 14, {}, agent="plain")
    fact(db, "demo.done", 15, {}, agent="child")
    task(db, "run-one", "hash1", owner="plain")
    db.commit()
    db.close()
    shown = json.loads(await MeasureStatusTool().execute(_thread_id="plain", kpis=True))
    items, runs = sorted(shown["kpis"], key=lambda row: row["name"])
    assert items["name"] == "items" and runs["name"] == "runs"
    assert items["last_complete"]["value"] == 2
    assert runs["last_complete"]["status"] != "not tracked" and "reason" not in runs["last_complete"]


@pytest.mark.asyncio
async def test_a_stored_kpi_the_evaluator_cannot_compute_reads_as_not_tracked_and_leaves_its_siblings(owner) -> None:
    seed_kpis(owner)
    db = get_db()
    definition(db, owner, "kpi", "bad", {"title": "Bad", "formula": "mean", "num": {"event": "demo.done"}, "min_n": 1})
    definition(db, owner, "kpi", "none", {"title": "None", "num": {"event": "demo.done"}, "min_n": 1})
    definition(db, owner, "kpi", "half", {"title": "Half", "formula": "ratio", "num": {"event": "demo.done"}, "min_n": 1})
    db.commit()
    db.close()
    shown = json.loads(await MeasureStatusTool().execute(_thread_id="owner", kpis=True))
    rows = {row["name"]: row for row in shown["kpis"]}
    then = " in the measures file, then run measure_sync"
    reasons = {"bad": 'formula "mean" is not one of count, ratio, rate_per_run, share, percentile; use one of them' + then,
               "none": "has no formula; add one of count, ratio, rate_per_run, share, percentile as its formula" + then,
               "half": "formula ratio needs den; add it" + then}
    for name, reason in reasons.items():
        for period in ("current", "last_complete"):
            assert rows[name][period]["status"] == "not tracked"
            assert rows[name][period]["reason"] == reason
    assert rows["items"]["last_complete"]["value"] == 2
