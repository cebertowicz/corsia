"""Presence-only attention behind minutes per outcome on the Agents table."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.measurement_agent_attention import query_root_active
from api.measurement_queries import prepare_outcome_periods, query_family
from core.measurement_scope import resolve_grain

LATEST = "2026-09-28"
SCOPE = {"scope_project": "all", "scope_agent": None, "user_id": "steve"}
GRAIN = resolve_grain("weeks", "UTC", now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
PARAMS = {**SCOPE, "grain": GRAIN}


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    for thread in ("root", "other"):
        connection.execute("INSERT INTO threads (id,user_id,title,agent_enabled,agent_status,created_at,updated_at) VALUES (?,'steve',?,1,'active','t','t')", (thread, thread))
    try:
        yield connection
    finally:
        connection.close()


def _visit(db, thread: str, entered_at: str, exited_at: str, *, visit_user: str = "steve", reason: str = "hidden") -> None:
    db.execute(
        "INSERT INTO presence_visits (user_id,surface,thread_id,entered_at,exited_at,seconds,sessions,exit_reason) "
        "VALUES (?, 'chat', ?, ?, ?, 0, 1, ?)",
        (visit_user, thread, entered_at, exited_at, reason),
    )


def _agent_rows(db, params):
    prepare_outcome_periods(db, params, roots=True)
    return {row["root_id"]: row["active_hours"] for row in query_root_active(db, params)}


def test_overlapping_threads_count_once_and_split_evenly_between_agents(db):
    _visit(db, "root", "2026-09-29T12:00:00Z", "2026-09-29T12:10:00Z")
    _visit(db, "other", "2026-09-29T12:05:00Z", "2026-09-29T12:15:00Z")
    rows = _agent_rows(db, {**PARAMS})
    prepare_outcome_periods(db, PARAMS)
    total = query_family(db, "active", PARAMS)
    assert total == [{"period": LATEST, "active_hours": pytest.approx(15 / 60), "outcomes": 0}]
    assert rows == {"root": pytest.approx(7.5 / 60), "other": pytest.approx(7.5 / 60)}
    assert sum(rows.values()) == pytest.approx(total[0]["active_hours"])


def test_agent_page_uses_the_same_equal_share_as_its_row(db):
    _visit(db, "root", "2026-09-29T12:00:00Z", "2026-09-29T12:10:00Z")
    _visit(db, "other", "2026-09-29T12:05:00Z", "2026-09-29T12:15:00Z")
    rows = _agent_rows(db, {**PARAMS})
    for agent, hours in rows.items():
        own = {**PARAMS, "scope_agent": agent}
        prepare_outcome_periods(db, own)
        assert query_family(db, "active", own)[0]["active_hours"] == pytest.approx(hours)


def test_agent_time_on_platform_uses_the_same_equal_share_as_attention(db):
    _visit(db, "root", "2026-09-29T12:00:00Z", "2026-09-29T12:10:00Z")
    _visit(db, "other", "2026-09-29T12:05:00Z", "2026-09-29T12:15:00Z")
    own = {**PARAMS, "scope_agent": "root"}
    prepare_outcome_periods(db, own)
    assert query_family(db, "presence", own)[0]["value"] == pytest.approx(7.5 / 60)
    assert query_family(db, "active", own)[0]["active_hours"] == pytest.approx(7.5 / 60)


@pytest.mark.parametrize(("zone", "entered_at", "exited_at"), [
    ("UTC", "2026-09-29T23:30:00Z", "2026-09-30T00:30:00Z"),
    ("Asia/Kolkata", "2026-09-29T18:00:00Z", "2026-09-29T19:00:00Z"),
])
def test_period_boundary_intervals_preserve_and_split_time(db, zone, entered_at, exited_at):
    grain = resolve_grain("days", zone, now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc))
    params = {**SCOPE, "grain": grain}
    _visit(db, "root", entered_at, exited_at)
    prepare_outcome_periods(db, params)
    rows = query_family(db, "active", params)
    assert {row["period"]: row["active_hours"] for row in rows} == {
        "2026-09-29": pytest.approx(0.5), "2026-09-30": pytest.approx(0.5),
    }


def test_presence_start_uses_one_global_source_for_days_and_weeks(db):
    from api.measurement_presence import first_presence_period

    _visit(db, "root", "2026-09-21T12:00:00Z", "2026-09-21T12:10:00Z")
    assert first_presence_period(db, "steve", resolve_grain("days", "UTC", now=datetime(2026, 10, 1, tzinfo=timezone.utc))) == "2026-09-21"
    assert first_presence_period(db, "steve", resolve_grain("weeks", "UTC", now=datetime(2026, 10, 1, tzinfo=timezone.utc))) == "2026-09-21"


def test_idle_exit_stops_accrual_at_the_recorded_exit(db):
    _visit(db, "root", "2026-09-29T12:00:00Z", "2026-09-29T12:10:00Z", reason="idle")
    prepare_outcome_periods(db, PARAMS)
    assert query_family(db, "active", PARAMS)[0]["active_hours"] == pytest.approx(10 / 60)


def test_visit_follows_referenced_thread_owner_not_visit_row_owner(db):
    _visit(db, "root", "2026-09-29T12:00:00Z", "2026-09-29T12:10:00Z", visit_user="imported-owner")
    prepare_outcome_periods(db, PARAMS)
    assert query_family(db, "active", PARAMS)[0]["active_hours"] == pytest.approx(10 / 60)


def test_pre_presence_periods_are_absent(db):
    prepare_outcome_periods(db, PARAMS)
    assert query_family(db, "active", PARAMS) == []
