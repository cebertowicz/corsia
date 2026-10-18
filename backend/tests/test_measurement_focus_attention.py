"""Measurement attention counts a visit only inside its focus spans."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tests.test_measurement_presence_scope import LEGACY_WEEK_FROM, LEGACY_WEEK_TO, _seed_thread

DAY = datetime(2026, 9, 21, 12, 0, 0)


def _iso(minutes: float) -> str:
    return (DAY + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _spans(*pairs: tuple[float, float]) -> str:
    return json.dumps([[_iso(a), _iso(b)] for a, b in pairs])


def _visit(db, thread: str, start: float, end: float, spans: str | None) -> None:
    db.execute(
        "INSERT INTO presence_visits (user_id,surface,thread_id,entered_at,exited_at,seconds,sessions,"
        "exit_reason,focus_spans) VALUES ('steve','chat',?,?,?,?,1,'navigated',?)",
        (thread, _iso(start), _iso(end), int((end - start) * 60), spans),
    )


def _open_visit(db, thread: str, start: float, exit_at: float | None, spans: str, since: float | None) -> None:
    db.execute(
        "INSERT INTO thread_presence (user_id,thread_id,present,last_entered_at,last_confirmed_at,updated_at,"
        "exit_pending_at,focus_spans,focus_since) VALUES ('steve',?,1,?,?,?,?,?,?)",
        (thread, _iso(start), _iso(start), _iso(start), None if exit_at is None else _iso(exit_at), spans,
         None if since is None else _iso(since)),
    )


def _family(*, agent: str | None = None) -> list:
    from api.measurement_queries import prepare_outcome_periods, query_family
    from infra.database import get_db
    from tests.test_measurement_api import _grained

    params = {"user_id": "steve", "scope_project": "all", "scope_agent": agent,
              "from": LEGACY_WEEK_FROM, "to": LEGACY_WEEK_TO}
    db = get_db()
    try:
        prepare_outcome_periods(db, _grained(params))
        return [row for row in query_family(db, "presence", _grained(params)) if str(row["period"]) >= LEGACY_WEEK_FROM]
    finally:
        db.close()


def _minutes(rows: list) -> float:
    return sum(row["value"] or 0 for row in rows) * 60


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    conn = get_db()
    try:
        _seed_thread(conn, "a", agent=True)
        _seed_thread(conn, "b", agent=True)
        yield conn
        conn.commit()
    finally:
        conn.close()


def test_a_visit_counts_only_inside_its_focus_spans(db):
    _visit(db, "a", 0, 60, _spans((0, 10), (30, 40)))
    db.commit()
    assert _minutes(_family()) == pytest.approx(20)


def test_a_visit_that_predates_focus_tracking_counts_whole(db):
    _visit(db, "a", 0, 60, None)
    db.commit()
    assert _minutes(_family()) == pytest.approx(60)


def test_a_visit_with_no_focus_counts_nothing(db):
    _visit(db, "a", 0, 60, "[]")
    db.commit()
    assert _minutes(_family()) == 0


def test_a_span_outside_its_visit_adds_nothing(db):
    _visit(db, "a", 0, 60, _spans((-30, 10), (50, 90)))
    db.commit()
    assert _minutes(_family()) == pytest.approx(20)


def test_overlapping_spans_of_two_threads_count_once(db):
    _visit(db, "a", 0, 60, _spans((0, 30)))
    _visit(db, "b", 0, 60, _spans((10, 40)))
    db.commit()
    assert _minutes(_family()) == pytest.approx(40)


def test_n_counts_visits_however_often_the_window_was_left(db):
    _visit(db, "a", 0, 100, _spans(*[(2 * i, 2 * i + 1) for i in range(50)]))
    _visit(db, "b", 0, 60, _spans((0, 5)))
    db.commit()
    [row] = _family()
    assert row["n"] == 2
    assert row["value"] * 60 == pytest.approx(52)  # 0-5 from both threads, then 47 min of one alone


def test_an_exit_pending_open_visit_stops_at_its_exit(db):
    _open_visit(db, "a", 0, 30, _spans((0, 10)), since=20)
    db.commit()
    assert _minutes(_family()) == pytest.approx(20)  # 0-10 and the open span cut at 12:30


def test_an_open_visit_without_a_focus_span_counts_only_its_closed_spans(db):
    _open_visit(db, "a", 0, 30, _spans((0, 10)), since=None)
    db.commit()
    assert _minutes(_family()) == pytest.approx(10)


def test_a_blurred_thread_takes_no_share_of_a_focused_threads_time(db):
    _visit(db, "a", 0, 60, _spans((0, 60)))
    _visit(db, "b", 0, 60, "[]")
    db.commit()
    assert _minutes(_family(agent="a")) == pytest.approx(60)
    assert _minutes(_family(agent="b")) == 0
    assert _minutes(_family()) == pytest.approx(60)


def test_threads_focused_together_split_the_slice(db):
    _visit(db, "a", 0, 60, _spans((0, 60)))
    _visit(db, "b", 0, 60, _spans((30, 60)))
    db.commit()
    assert _minutes(_family(agent="a")) == pytest.approx(45)
    assert _minutes(_family(agent="b")) == pytest.approx(15)
