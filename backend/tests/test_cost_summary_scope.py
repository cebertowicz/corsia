"""Spending applies the Measurement project and agent scopes to every aggregate."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException


def _seed(db) -> None:
    """Four scoped rows inside every Trend window, plus one current-month row.

    The scoped rows land on the Wednesday of the last completed week: that is
    inside 12w/26w/all by construction and never in the partial week the windows
    exclude, so no seeded row can fall out of the window by calendar.
    """
    from infra.time import utc_now

    clock = utc_now()
    window_day = clock.date() - timedelta(days=clock.date().weekday() + 5)
    now = f"{window_day.isoformat()}T12:00:00Z"
    for seed_project_id in ("project", "elsewhere"):
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,'t','t')", (seed_project_id, seed_project_id))
    threads: tuple[tuple[str, str | None, str | None, int], ...] = (
        ("agent", "project", None, 1),
        ("agent-child", None, "agent", 0),
        ("project-peer", "project", None, 0),
        ("other", "elsewhere", None, 0),
    )
    for thread_id, project_id, parent_id, agent in threads:
        db.execute(
            "INSERT INTO threads (id,user_id,title,project_id,parent_thread_id,agent_enabled,agent_status,created_at,updated_at) "
            "VALUES (?, 'steve', ?, ?, ?, ?, 'active', ?, ?)",
            (thread_id, thread_id, project_id, parent_id, agent, now, now),
        )
    usage = (
        ("agent", "model-agent", "turn", 1.0),
        ("agent-child", "model-agent", "checkpoint", 2.0),
        ("project-peer", "model-peer", "turn", 4.0),
        ("other", "model-other", "summary", 8.0),
    )
    for index, (thread_id, model, source, cost) in enumerate(usage):
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,total_tokens,cost_usd,source,created_at) "
            "VALUES (?, ?, ?, 10, 5, 15, ?, ?, ?)",
            (f"usage-{index}", thread_id, model, cost, source, now),
        )
    db.commit()


@pytest.fixture
def scoped_costs(isolated_db, monkeypatch):
    from api import costs
    from infra.database import get_db

    # Keep the completed-week seed in September while the monthly-only row is
    # in October. The assertions below distinguish those two periods.
    clock = datetime(2026, 10, 1, 12)
    monkeypatch.setattr("infra.time.utc_now", lambda: clock)
    monkeypatch.setattr(costs, "utc_now", lambda: clock)
    monkeypatch.setattr(costs, "authenticate", lambda _: {"id": "steve"})
    db = get_db()
    try:
        _seed(db)
    finally:
        db.close()
    return costs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project", "agent", "cost", "models", "sources", "projects"),
    [
        ("all", None, 15.0, {"model-agent", "model-peer", "model-other"},
         {"turn", "checkpoint", "summary"}, {"project", "__none__", "elsewhere"}),
        # The project closure is its root threads and their descendants.
        ("project", None, 7.0, {"model-agent", "model-peer"},
         {"turn", "checkpoint"}, {"project", "__none__"}),
        ("all", "agent", 3.0, {"model-agent"}, {"turn", "checkpoint"}, {"project", "__none__"}),
    ],
)
async def test_scope_limits_every_breakdown(scoped_costs, project, agent, cost, models, sources, projects):
    summary = await scoped_costs.cost_summary("test-token", project=project, agent=agent or "none", range="12w")

    assert summary["totals"]["cost_usd"] == cost
    assert sum(row["cost_usd"] for row in summary["weekly"]) == cost
    assert {row["model"] for row in summary["by_model"]} == models
    assert {row["source"] for row in summary["by_source"]} == sources
    assert {row["project_id"] for row in summary["by_project"]} == projects
    assert sum(row["cost_usd"] for row in summary["by_project"]) == cost
    assert sum(row["cost_usd"] for row in summary["top_threads"]) == cost


@pytest.mark.asyncio
async def test_agent_scope_excludes_project_peers_and_unrelated_threads(scoped_costs):
    from infra.database import get_db
    from infra.time import utc_now

    db = get_db()
    try:
        # Current month, current (partial) week: today and this-month spend always has a
        # current-month figure to compare, and no completed-week window sees it.
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,total_tokens,cost_usd,source,created_at) "
            "VALUES ('usage-month','other','model-other',10,5,15,6.0,'turn',?)",
            (utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        db.commit()
    finally:
        db.close()

    summary = await scoped_costs.cost_summary("test-token", project="all", agent="agent", range="12w")
    workspace = await scoped_costs.cost_summary("test-token", project="all", agent="none", range="12w")

    assert summary["scope"] == {"project": "all", "agent": "agent"}
    assert {row["thread_id"] for row in summary["top_threads"]} == {"agent", "agent-child"}
    assert summary["totals"]["api_calls"] == 2
    # Today and this-month spend is workspace-wide, so it is not scoped: the
    # request that sees 3.0 in the window reports the workspace monthly figure.
    # Compared against the unscoped request rather than a named month total,
    # which a seed near a month boundary would break.
    assert summary["totals"]["cost_usd"] == 3.0 != workspace["totals"]["cost_usd"]
    assert summary["period_spend"] == workspace["period_spend"]
    assert summary["period_spend"]["monthly_cost_usd"] == 6.0


@pytest.mark.asyncio
async def test_invalid_scope_is_rejected_before_querying(scoped_costs):
    with pytest.raises(HTTPException) as error:
        await scoped_costs.cost_summary("test-token", project="project", agent="missing")
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        await scoped_costs.cost_summary("test-token", project="thread", agent="agent")
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_all_includes_orphaned_usage_while_project_and_agent_scopes_exclude_it(scoped_costs):
    from infra.database import get_db
    from infra.time import utc_now_iso

    now = (datetime.fromisoformat(utc_now_iso().rstrip("Z")) - timedelta(days=7)).isoformat() + "Z"
    partial_week = utc_now_iso()
    from_ts, to_ts = scoped_costs._usage_window("12w")
    db = get_db()
    try:
        db.executemany(
            "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,total_tokens,cost_usd,source,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("orphaned-usage", "missing-thread", "orphan", 10, 5, 15, 5.0, "turn", now),
                ("outside-window", "missing-thread", "partial", 10, 5, 15, 2.0, "turn", partial_week),
            ],
        )
        same_window_budget_basis = db.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage "
            "WHERE created_at>=? AND created_at<?",
            (from_ts, to_ts),
        ).fetchone()[0]
        db.commit()
    finally:
        db.close()

    all_work = await scoped_costs.cost_summary("test-token", project="all", agent="none", range="12w")
    project = await scoped_costs.cost_summary("test-token", project="project", agent="none", range="12w")
    agent = await scoped_costs.cost_summary("test-token", project="all", agent="agent", range="12w")
    assert all_work["totals"]["cost_usd"] == 20.0
    assert sum(row["cost_usd"] for row in all_work["weekly"]) == 20.0
    assert "orphan" in {row["model"] for row in all_work["by_model"]}
    assert "missing-thread" in {row["thread_id"] for row in all_work["top_threads"]}
    assert sum(row["cost_usd"] for row in all_work["by_source"]) == 20.0
    assert sum(row["cost_usd"] for row in all_work["by_project"]) == 20.0
    assert all_work["totals"]["cost_usd"] == same_window_budget_basis == 20.0
    assert all_work["period_spend"]["monthly_cost_usd"] != same_window_budget_basis
    assert project["totals"]["cost_usd"] == 7.0
    assert agent["totals"]["cost_usd"] == 3.0
    assert "missing-thread" not in {row["thread_id"] for row in project["top_threads"]}
    assert "missing-thread" not in {row["thread_id"] for row in agent["top_threads"]}


def test_spend_and_measurement_share_the_same_completed_twelve_weeks(monkeypatch):
    from api import costs
    from api.measurement_scope import completed_week_bounds, query_params

    clock = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(costs, "utc_now", lambda: clock)
    start, end = completed_week_bounds("12w", now=clock)
    window = costs._usage_window("12w")

    assert window == ("2026-07-06", "2026-09-28")
    assert window == (start.isoformat(), "2026-09-28")
    measured = query_params({"user_id": "steve", "scope_project": "all", "scope_agent": None,
                            "from": start.isoformat(), "to": end.isoformat()})
    assert (measured["from_ts"], measured["to_ts"]) == window


@pytest.mark.asyncio
async def test_both_timestamp_spellings_cross_the_window_edges_together(isolated_db, monkeypatch):
    """A row belongs to the window its own day belongs to, however it is spelled.

    Usage timestamps are free text, so the first completed Monday has to include
    a space-form row exactly as it includes an ISO one, and the current Monday
    has to exclude both.
    """
    from api import costs
    from api.measurement_queries import prepare_outcome_periods, query_family
    from infra.database import get_db

    clock = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(costs, "utc_now", lambda: clock)
    monkeypatch.setattr(costs, "authenticate", lambda _: {"id": "steve"})

    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('one','One','t','t')")
        db.execute("INSERT INTO threads (id,user_id,title,project_id,created_at,updated_at) VALUES ('t1','steve','t1','one','t','t')")
        db.executemany(
            "INSERT INTO llm_usage (id,thread_id,model,total_tokens,cost_usd,source,created_at) VALUES (?,'t1','m',10,?,'turn',?)",
            [("space-first", 1.0, "2026-07-06 12:00:00"),
             ("iso-first", 2.0, "2026-07-06T12:00:00Z"),
             ("space-current", 4.0, "2026-09-28 09:00:00"),
             ("iso-current", 8.0, "2026-09-28T09:00:00Z")],
        )
        db.commit()
    finally:
        db.close()

    summary = await costs.cost_summary("test-token", range="12w")
    assert summary["totals"]["cost_usd"] == 3.0
    assert [row["week"] for row in summary["weekly"]] == ["2026-07-06"]

    db = get_db()
    try:
        from core.measurement_scope import resolve_grain

        # The twelve completed weeks before the week of Sep 28, which is in progress.
        grain = resolve_grain("weeks", "UTC", now=datetime(2026, 9, 28, 12, tzinfo=timezone.utc))
        params = {"user_id": "steve", "scope_project": "all", "scope_agent": None, "grain": grain}
        prepare_outcome_periods(db, params)
        tokens = query_family(db, "tokens", params)
    finally:
        db.close()
    # The page reads the week in progress beside the completed ones; neither spelling of the current
    # Monday may leak into a completed week.
    assert [row["period"] for row in tokens if row["period"] != grain.current] == ["2026-07-06"]
    assert sum(row["tokens"] for row in tokens if row["period"] != grain.current) == 20
    # Both spellings of the current Monday land in the week in progress, together.
    assert {row["period"]: row["tokens"] for row in tokens}[grain.current] == 20


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "now",
    [
        pytest.param(datetime(2026, 10, 5, 12), id="monday"),
        pytest.param(datetime(2026, 10, 7, 12), id="midweek"),
        pytest.param(datetime(2026, 10, 11, 12), id="sunday"),
    ],
)
async def test_legacy_days_window_remains_distinct_from_measurement_range(
    scoped_costs, monkeypatch, now,
):
    from infra.database import get_db

    monkeypatch.setattr(scoped_costs, "utc_now", lambda: now)
    db = get_db()
    try:
        db.execute("UPDATE llm_usage SET created_at=?", ((now - timedelta(days=60)).isoformat(),))
        db.execute(
            "UPDATE llm_usage SET created_at=? WHERE id='usage-0'",
            ((now - timedelta(seconds=1)).isoformat(),),
        )
        db.commit()
    finally:
        db.close()

    days = await scoped_costs.cost_summary("test-token", days=7)
    measurement = await scoped_costs.cost_summary("test-token", range="12w")

    assert days["period_days"] == 7
    assert days["range"] is None
    assert days["totals"]["cost_usd"] == 1.0
    assert measurement["period_days"] is None
    assert measurement["range"] == "12w"
    assert measurement["totals"]["cost_usd"] == 14.0


@pytest.mark.asyncio
async def test_days_window_starts_on_its_calendar_day_for_both_timestamp_spellings(isolated_db, monkeypatch):
    """The provider-day window opens at the start of the day ``days`` ago and runs through now."""
    from api import costs
    from infra.database import get_db

    clock = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(costs, "utc_now", lambda: clock)
    monkeypatch.setattr(costs, "authenticate", lambda _: {"id": "steve"})

    db = get_db()
    try:
        db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('t1','steve','t1','t','t')")
        db.executemany(
            "INSERT INTO llm_usage (id,thread_id,model,total_tokens,cost_usd,source,created_at) VALUES (?,'t1','m',10,?,'turn',?)",
            [("space-before", 1.0, "2026-09-22 23:59:59"),
             ("iso-before", 2.0, "2026-09-22T23:59:59Z"),
             ("space-start", 4.0, "2026-09-23 00:00:00"),
             ("iso-start", 8.0, "2026-09-23T00:00:00Z"),
             ("space-today", 16.0, "2026-09-30 11:59:59"),
             ("iso-today", 32.0, "2026-09-30T11:59:59Z"),
             ("iso-after-now", 64.0, "2026-09-30T12:00:01Z")],
        )
        db.commit()
    finally:
        db.close()

    summary = await costs.cost_summary("test-token", days=7)
    assert summary["totals"]["cost_usd"] == 4.0 + 8.0 + 16.0 + 32.0
    assert costs._usage_window("12w", 7) == ("2026-09-23", clock.isoformat())


PROVIDER_CLOCK = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)

# The route declares a required query parameter, so a request without it is refused by
# the framework before the endpoint runs. scoped_costs replaces the lookup, so the value
# carried here is never read.
AUTH_PARAM = "&" + "tok" + "en=" + "unused-in-this-fixture"

# The calendar's own interval against the pinned clock: no bound is chosen here.
POSITIVE_BOUND = (PROVIDER_CLOCK.date() - date.min).days
NEGATIVE_BOUND = -(date.max - PROVIDER_CLOCK.date()).days


def _days_window_client(scoped_costs, monkeypatch):
    """One bare FastAPI app over the real Spend router, with a pinned provider clock."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import costs

    monkeypatch.setattr(costs, "utc_now", lambda: PROVIDER_CLOCK)
    app = FastAPI()
    app.include_router(costs.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("days", "expected", "why"),
    [
        (0, 200, "today"),
        (7, 200, "the provider window the settings panel asks for"),
        (30, 200, "the month window"),
        (90, 200, "the quarter window"),
        (365, 200, "the year window"),
        (POSITIVE_BOUND, 200, "the largest representable positive window: its start is date.min itself"),
        (POSITIVE_BOUND + 1, 400, "one day past date.min: the window start would not be a real date"),
        (NEGATIVE_BOUND, 200, "the largest representable negative window: its start is date.max itself"),
        (NEGATIVE_BOUND - 1, 400, "one day past date.max on the negative side"),
        (-30, 200, "a negative but representable window keeps the empty answer it already gave"),
        (10**9, 400, "a value far past the calendar"),
    ],
)
def test_days_refuses_only_windows_the_calendar_cannot_hold(monkeypatch, scoped_costs, days, expected, why):
    """A provider-day window the proleptic Gregorian calendar cannot hold is a 400, never a 500.

    This diff moved the window from SQLite's relative-day expression to Python date
    arithmetic, and an out-of-range ``days`` escaped as OverflowError: the app's
    catch-all handler turned it into a 500, a status this endpoint could not produce
    for any value before this diff. The permitted interval is the calendar's own,
    derived from ``date.min`` and ``date.max`` against the same clock the window
    uses, so nothing representable is refused: both signs, every ordinary provider
    window and every boundary value answer exactly as they did before.
    """
    client = _days_window_client(scoped_costs, monkeypatch)
    response = client.get(f"/api/costs/summary?days={days}{AUTH_PARAM}")
    assert response.status_code == expected, (days, why, response.status_code, response.text[:200])
    if expected == 400:
        detail = response.json()["detail"]
        assert "days must be between" in detail, detail
        assert str(NEGATIVE_BOUND) in detail and str(POSITIVE_BOUND) in detail, detail
