"""The one Measurement scope and Trend range contract: completed periods and rejections."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException

from api.measurement_scope import completed_week_bounds, parse_trend_range, scope_cte, validate_scope

# A Wednesday: its own week is partial, so the last completed week is the Monday
# before it. Every bounds assertion below pins this clock, never the real one.
WEDNESDAY = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
LAST_COMPLETED_WEEK = date(2026, 9, 21)


def test_completed_week_bounds_exclude_the_partial_current_period():
    assert completed_week_bounds("12w", now=WEDNESDAY) == (date(2026, 7, 6), LAST_COMPLETED_WEEK)
    assert completed_week_bounds("26w", now=WEDNESDAY) == (date(2026, 3, 30), LAST_COMPLETED_WEEK)


def test_all_range_starts_at_the_oldest_metric_definition_instead_of_empty_time():
    assert completed_week_bounds("all", date(2026, 1, 5), now=WEDNESDAY) == (date(2026, 1, 5), LAST_COMPLETED_WEEK)


def test_all_range_starts_on_the_monday_of_a_mid_period_data_start():
    """A metric's data can start mid-week; the spine it anchors is still Mondays.

    Weekly rows are keyed by their Monday, so a spine stepped from a Sunday or a
    Thursday matches none of them and serves every period as unmeasured.
    """
    assert completed_week_bounds("all", date(2026, 8, 16), now=WEDNESDAY) == (date(2026, 8, 10), LAST_COMPLETED_WEEK)
    assert completed_week_bounds("all", date(2026, 9, 10), now=WEDNESDAY) == (date(2026, 9, 7), LAST_COMPLETED_WEEK)


def test_all_range_for_data_that_starts_after_the_last_completed_week_is_empty():
    """Nothing is measured yet, so the window holds no period: no row is invented."""
    start, end = completed_week_bounds("all", date(2026, 10, 1), now=WEDNESDAY)
    assert (start, end) == (date(2026, 9, 28), LAST_COMPLETED_WEEK)
    assert start > end


def test_trend_range_rejects_any_window_the_surface_does_not_offer():
    assert parse_trend_range("26w") == "26w"
    for value in ("4w", "period", "", "12W"):
        with pytest.raises(HTTPException) as error:
            parse_trend_range(value)
        assert error.value.status_code == 400


def _seeded(db) -> None:
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('one','One','t','t'),('two','Two','t','t')")
    db.execute(
        "INSERT INTO threads (id,user_id,title,project_id,agent_enabled,agent_status,created_at,updated_at) VALUES "
        "('root-one','steve','Root','one',0,'active','t','t'),"
        "('agent-one','steve','Agent one','one',1,'active','t','t'),"
        "('agent-two','steve','Agent two','two',1,'active','t','t'),"
        "('agent-retired','steve','Retired','one',1,'inactive','t','t')"
    )
    db.commit()


def test_scope_rejects_an_agent_from_another_project(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        _seeded(db)
        with pytest.raises(HTTPException) as error:
            validate_scope(db, "steve", "one", "agent-two")
    finally:
        db.close()
    assert error.value.status_code == 400


def test_scope_rejects_an_unknown_project_or_agent(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        _seeded(db)
        with pytest.raises(HTTPException) as unknown_project:
            validate_scope(db, "steve", "absent", None)
        with pytest.raises(HTTPException) as retired_agent:
            validate_scope(db, "steve", "all", "agent-retired")
        with pytest.raises(HTTPException) as unknown_agent:
            validate_scope(db, "steve", "all", "nobody")
        with pytest.raises(HTTPException) as blank_agent:
            validate_scope(db, "steve", "all", "")
    finally:
        db.close()
    assert {unknown_project.value.status_code, retired_agent.value.status_code, unknown_agent.value.status_code} == {404}
    assert blank_agent.value.status_code == 400


def test_none_is_the_explicit_no_agent_selection_rather_than_a_missing_one(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        _seeded(db)
        selected = validate_scope(db, "steve", "one", "none")
    finally:
        db.close()
    assert selected.response() == {"project": "one", "agent": None}
    assert selected.params("steve") == {"user_id": "steve", "scope_project": "one", "scope_agent": None}


def _seed_family(db) -> None:
    """Seed the topology the roster actually allows.

    A thread becomes an agent by being given a responsibility, whatever family
    it sits in, so an active agent can legitimately carry a parent.
    """
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('one','One','t','t'),('two','Two','t','t')")
    db.execute(
        "INSERT INTO threads (id,user_id,title,project_id,parent_thread_id,agent_enabled,agent_status,created_at,updated_at) VALUES "
        "('top-one','steve','Top one','one',NULL,0,'active','t','t'),"
        "('agent-child','steve','Agent child','one','top-one',1,'active','t','t'),"
        "('agent-descendant','steve','Agent descendant','one','agent-child',0,'active','t','t'),"
        "('top-two','steve','Top two','two',NULL,0,'active','t','t'),"
        "('child-two','steve','Child two','two','top-two',0,'active','t','t')"
    )
    db.commit()


def test_the_retired_scope_pair_is_refused_rather_than_widened():
    """An agent addressed the retired way must not become workspace scope."""
    from api.measurement_scope import query_params

    with pytest.raises(ValueError):
        query_params({"user_id": "steve", "scope_kind": "agent", "scope_id": "agent-child",
                      "from": "2026-07-06", "to": "2026-09-21"})
    kept = query_params({"user_id": "steve", "scope_project": "one", "scope_agent": "agent-child",
                         "from": "2026-07-06", "to": "2026-09-21"})
    assert (kept["scope_project"], kept["scope_agent"]) == ("one", "agent-child")


@pytest.mark.parametrize("scope", [
    {},
    {"scope_project": "one"},
    {"scope_agent": None},
    {"scope_project": "", "scope_agent": None},
    {"scope_project": None, "scope_agent": None},
    {"scope_project": "one", "scope_agent": ""},
])
def test_a_missing_or_empty_scope_is_refused_rather_than_widened(scope):
    """Neither half of the composite pair has a default: none is not all."""
    from api.measurement_scope import query_params

    with pytest.raises(ValueError):
        query_params({"user_id": "steve", "from": "2026-07-06", "to": "2026-09-21", **scope})


def test_the_whole_workspace_is_a_scope_that_has_to_be_named():
    from api.measurement_scope import query_params

    named = query_params({"user_id": "steve", "scope_project": "all", "scope_agent": None,
                          "from": "2026-07-06", "to": "2026-09-21"})
    assert (named["scope_project"], named["scope_agent"]) == ("all", None)


def _closure(db, project: str, agent: str | None) -> set[str]:
    from infra.database import get_db

    db = db or get_db()
    params = validate_scope(db, "steve", project, agent).params("steve")
    return {row["id"] for row in db.execute(scope_cte() + "SELECT id FROM scope_threads", params).fetchall()}


def test_a_selected_agent_is_its_own_closure_root_inside_a_thread_family(isolated_db):
    """Selecting an agent scopes to that agent and its descendants only.

    The root must be the agent itself: it is addressed by id, so requiring a
    top-level parent would validate the agent and then return an empty scope.
    """
    from infra.database import get_db

    db = get_db()
    try:
        _seed_family(db)
        assert _closure(db, "one", "agent-child") == {"agent-child", "agent-descendant"}
        assert _closure(db, "all", "agent-child") == {"agent-child", "agent-descendant"}
        # The closure states its own project rule rather than leaning on validation.
        mismatched = {"user_id": "steve", "scope_project": "two", "scope_agent": "agent-child"}
        assert db.execute(scope_cte() + "SELECT id FROM scope_threads", mismatched).fetchall() == []
    finally:
        db.close()


def test_project_scope_still_starts_only_at_top_level_threads(isolated_db):
    """Without a selected agent, a project closure grows from its top-level roots."""
    from infra.database import get_db

    db = get_db()
    try:
        _seed_family(db)
        assert _closure(db, "one", None) == {"top-one", "agent-child", "agent-descendant"}
        assert _closure(db, "all", None) == {"top-one", "agent-child", "agent-descendant", "top-two", "child-two"}
    finally:
        db.close()
