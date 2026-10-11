"""Presence follows the selected scope: project-level visits only for a project-only query."""

from __future__ import annotations

import inspect

import pytest

USER = {"id": "steve"}
LEGACY_WEEK_FROM = "2026-09-14"
LEGACY_WEEK_TO = "2026-09-21"


def _seed_thread(db, thread_id: str, *, project_id: str | None = None,
                 parent_id: str | None = None, agent: bool = False) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,title,project_id,parent_thread_id,agent_enabled,agent_status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,'2026-09-01T00:00:00Z','2026-09-01T00:00:00Z')",
        (thread_id, "steve", thread_id, project_id, parent_id, int(agent), "active"),
    )


def _seed_visit(db, surface: str, *, thread_id: str | None = None, project_id: str | None = None,
                user_id: str = "steve") -> None:
    db.execute(
        "INSERT INTO presence_visits (user_id,surface,thread_id,project_id,entered_at,exited_at,seconds,sessions,exit_reason) "
        "VALUES (?,?,?,?,'2026-09-21T12:00:00Z','2026-09-21T13:00:00Z',3600,1,'hidden')",
        (user_id, surface, thread_id, project_id),
    )


def _seeded() -> None:
    """One hour of presence in each shape a scope can reach, inside project one."""
    from infra.database import get_db

    db = get_db()
    try:
        for project_id in ("one", "two"):
            db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,'t','t')", (project_id, project_id))
        _seed_thread(db, "agent-a", project_id="one", agent=True)
        _seed_thread(db, "agent-a-child", parent_id="agent-a")
        _seed_thread(db, "agent-b", project_id="one", agent=True)
        _seed_thread(db, "agent-c", project_id="two", agent=True)
        _seed_visit(db, "own-thread", thread_id="agent-a")
        _seed_visit(db, "own-child-thread", thread_id="agent-a-child")
        _seed_visit(db, "peer-agent-thread", thread_id="agent-b")
        _seed_visit(db, "project-level", project_id="one")
        _seed_visit(db, "other-project-thread", thread_id="agent-c")
        _seed_visit(db, "other-project-level", project_id="two")
        db.commit()
    finally:
        db.close()


def _composite_surface(measurement_series) -> bool:
    """Whether the handler addresses one scope with the composite project/agent/grain surface."""
    parameters = inspect.signature(measurement_series).parameters
    return {"project", "agent", "grain"} <= set(parameters)


async def _hours(measurement_series, *, project: str = "all", agent: str | None = None) -> float:
    """Total presence hours one composite scope selects, at the family the panel reads.

    Read through the family rather than ``/series`` on purpose: this file owns
    which visits a scope selects -- the same predicate the panel reads through --
    while the window is named here directly.
    """
    assert _composite_surface(measurement_series), (
        "presence scope is only a testable contract once one request carries project, agent and grain"
    )
    from api.measurement_queries import prepare_outcome_periods
    from infra.database import get_db
    from tests.test_measurement_api import _family_in_range, _grained

    params = {
        "user_id": USER["id"],
        "scope_project": project,
        "scope_agent": agent or None,
        "from": LEGACY_WEEK_FROM,
        "to": LEGACY_WEEK_TO,
    }
    db = get_db()
    try:
        prepare_outcome_periods(db, _grained(params))
        return sum(row["value"] or 0 for row in _family_in_range(db, "presence", params))
    finally:
        db.close()


@pytest.mark.asyncio
async def test_project_only_scope_keeps_project_level_presence(isolated_db):
    """The baseline a project-only query must keep: its own closure plus project-level visits."""
    from api.measurement import measurement_series

    _seeded()
    # Four concurrent visits occupy one hour of the person's time, not four hours.
    assert await _hours(measurement_series, project="one") == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_shared_project_does_not_include_another_users_project_only_presence(isolated_db):
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('shared','shared','t','t')")
        _seed_thread(db, "steves-thread", project_id="shared")
        _seed_visit(db, "other-user-project-level", project_id="shared", user_id="other")
        db.commit()
    finally:
        db.close()

    assert await _hours(measurement_series) == 0
    assert await _hours(measurement_series, project="shared") == 0


@pytest.mark.asyncio
async def test_agent_scope_counts_only_the_selected_agent_closure(isolated_db):
    """A selected agent never inherits another agent's or unassigned project presence."""
    from api.measurement import measurement_series

    _seeded()
    # Each threaded slice is split evenly, so this closure keeps its two allocated shares.
    assert await _hours(measurement_series, agent="agent-a") == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_project_and_agent_together_keep_the_exact_agent_closure(isolated_db):
    """Naming an agent's own project must not change that agent's answer.

    Pre-fix, the presence predicate took its project alternative for any named
    project, adding the visit recorded against the project with no thread. The
    same agent then answered different allocated time depending on whether its
    own project was named, while the agent-only and workspace+agent selections
    stayed correct, which is why a scope-only test missed it.
    """
    from api.measurement import measurement_series

    _seeded()
    assert await _hours(measurement_series, project="one", agent="agent-a") == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_workspace_scope_with_an_agent_excludes_project_level_presence(isolated_db):
    from api.measurement import measurement_series

    _seeded()
    assert await _hours(measurement_series, project="all", agent="agent-a") == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_exclusive_scope_surface_answered_only_the_agent(isolated_db):
    """Why the composite case was invisible before: the old surface had no project to name.

    A caller that wanted one agent inside one project could only name the agent,
    and the project was dropped without complaint, so the wrong-hours case could
    not be requested at all.
    """
    from api.measurement import measurement_series

    if _composite_surface(measurement_series):
        pytest.skip("the exclusive scope surface this records is retired on this branch")

    _seeded()
    response = await measurement_series(
        ids="time_on_platform_h", from_=LEGACY_WEEK_FROM, to=LEGACY_WEEK_TO,
        scope="agent", scope_id="agent-a", project="one", grain="weeks", timezone_name="UTC", user=USER,
    )
    hours = sum(row["value"] or 0 for row in response["rows"] if row["series"] == "time_on_platform_h")
    assert hours == 2.0


@pytest.mark.asyncio
async def test_presence_n_counts_the_scope_visits_not_timeline_slices(isolated_db):
    """``n`` behind time on platform is the visits observed, however they overlap."""
    from api.measurement_queries import prepare_outcome_periods
    from infra.database import get_db
    from tests.test_measurement_api import _family_in_range, _grained

    _seeded()
    params = {"user_id": USER["id"], "scope_project": "one", "scope_agent": None,
              "from": LEGACY_WEEK_FROM, "to": LEGACY_WEEK_TO}
    db = get_db()
    try:
        prepare_outcome_periods(db, _grained(params))
        rows = _family_in_range(db, "presence", params)
    finally:
        db.close()
    assert [(row["value"], row["n"]) for row in rows] == [(pytest.approx(1.0), 4)]


def test_presence_coverage_is_one_registry_attribute():
    """Every series whose only instrument is presence starts at the first recorded presence."""
    from api.measurement_registry import REGISTRY, period_measured

    presence_only = {name for name, item in REGISTRY.items() if item.family in {"active", "presence"}}
    assert presence_only == {name for name, item in REGISTRY.items() if item.presence_backed}
    for name in presence_only:
        assert not period_measured(name, "2026-09-07", first_presence_period="2026-09-14")
        assert period_measured(name, "2026-09-14", first_presence_period="2026-09-14")
    # A total withholds nothing in a measured period: no presence there is a true zero.
    assert REGISTRY["time_on_platform_h"].minimum_n == 0


@pytest.mark.asyncio
async def test_an_exit_pending_live_visit_ends_where_the_user_left(isolated_db):
    """A live visit already marked exit-pending counts only up to its back-dated exit,
    the same instant the sweep will record, so a served period never shrinks later."""
    from api.measurement_queries import prepare_outcome_periods
    from infra.database import get_db
    from tests.test_measurement_api import _family_in_range, _grained

    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('one','one','t','t')")
        _seed_thread(db, "agent-a", project_id="one", agent=True)
        db.execute(
            "INSERT INTO thread_presence (user_id,thread_id,present,last_entered_at,exit_pending_at,exit_pending_since,"
            "exit_pending_reason,last_confirmed_at,updated_at) VALUES ('steve','agent-a',1,'2026-09-21T12:00:00Z',"
            "'2026-09-21T12:30:00Z','2026-09-21T12:32:00Z','hidden','2026-09-21T12:30:00Z','2026-09-21T12:32:00Z')"
        )
        db.commit()
        params = {"user_id": USER["id"], "scope_project": "one", "scope_agent": None,
                  "from": LEGACY_WEEK_FROM, "to": LEGACY_WEEK_TO}
        prepare_outcome_periods(db, _grained(params))
        rows = _family_in_range(db, "presence", params)
    finally:
        db.close()
    assert [row["value"] for row in rows] == [pytest.approx(0.5)]


@pytest.mark.asyncio
async def test_a_project_level_visit_counts_before_the_user_has_a_thread_there(isolated_db):
    """A project-level visit belongs to its recorded user, as coverage says; a project with
    no thread of theirs yet is still a real scope with that time in it."""
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('fresh','fresh','t','t')")
        _seed_visit(db, "project-level", project_id="fresh")
        db.commit()
    finally:
        db.close()
    assert await _hours(measurement_series, project="fresh") == pytest.approx(1.0)


def test_a_measured_period_with_outcomes_and_no_presence_counts_at_zero_time(isolated_db):
    """Inside coverage no presence is a true zero: the outcomes are served and pooled, with no time."""
    from api.measurement import _row
    from api.measurement_queries import prepare_outcome_periods
    from infra.database import get_db
    from tests.test_measurement_api import _family_in_range, _grained, _seed_outcomes

    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('one','one','t','t')")
        _seed_thread(db, "agent-a", project_id="one", agent=True)
        _seed_outcomes(db, "agent-a", count=25)
        db.commit()
        params = {"user_id": USER["id"], "scope_project": "all", "scope_agent": None,
                  "from": LEGACY_WEEK_TO, "to": LEGACY_WEEK_TO}
        prepare_outcome_periods(db, _grained(params))
        rows = _family_in_range(db, "active", params)
    finally:
        db.close()
    assert [(row["period"], row["active_hours"], row["outcomes"]) for row in rows] == [(LEGACY_WEEK_TO, 0, 25)]
    served = _row("active_hours_per_outcome", LEGACY_WEEK_TO, rows[0], False, first_presence_period=LEGACY_WEEK_FROM)
    assert (served["value"], served["n"]) == (0.0, 25)


def test_agent_minutes_pool_a_measured_period_without_presence_as_zero():
    from api.measurement_agents import _active_minutes, _pooled_value
    from api.measurement_registry import REGISTRY

    window = [{"period": "2026-09-21", "outcomes": 20, "active_hours": 2.0},
              {"period": "2026-09-28", "outcomes": 20, "active_hours": None}]
    assert _pooled_value(REGISTRY, "active_hours_per_outcome", window) == pytest.approx(0.05)
    assert _active_minutes(window, lambda _metric, _period: True) == pytest.approx(120.0)


def test_the_index_and_the_series_start_coverage_on_the_same_local_day(isolated_db):
    """A first visit just after UTC midnight is the previous evening for a Pacific viewer."""
    from api.measurement_presence import first_presence_period
    from api.measurement_scope import resolve_grain
    from infra.database import get_db
    from tests.test_measurement_api import _measurement_client

    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('one','one','t','t')")
        db.execute(
            "INSERT INTO presence_visits (user_id,surface,thread_id,project_id,entered_at,exited_at,seconds,sessions,exit_reason) "
            "VALUES ('steve','project-level',NULL,'one','2026-09-21T03:00:00Z','2026-09-21T04:00:00Z',3600,1,'hidden')"
        )
        db.commit()
        series_start = first_presence_period(db, "steve", resolve_grain("days", "America/Los_Angeles"))
    finally:
        db.close()
    response = _measurement_client().get(
        "/api/measurement/series/index?project=all&agent=none&timezone=America/Los_Angeles")
    assert response.status_code == 200, response.text
    starts = {item["id"]: item["data_start"] for item in response.json()["definitions"]}
    assert series_start == "2026-09-20"
    assert starts["active_hours_per_outcome"] == starts["time_on_platform_h"] == series_start
    assert starts["estimated_human_hours_per_active_hour"] == "2026-09-20"
