
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import infra.analytics as analytics  # noqa: E402
import infra.analytics_db as analytics_db  # noqa: E402
import infra.config as config  # noqa: E402
import infra.product_events as product_events  # noqa: E402




@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "analytics.duckdb"
    monkeypatch.setattr(config, "ANALYTICS_DB_PATH", db_path)
    monkeypatch.setattr(analytics_db, "ANALYTICS_DB_PATH", db_path)
    product_events._reset_for_tests()
    yield db_path
    product_events._reset_for_tests()


def _seed(rows: list[tuple]) -> None:
    conn = analytics_db.get_db()
    for ts, category, action, properties, session_id, project_id in rows:
        conn.execute(
            "INSERT INTO product_events VALUES (?, ?, ?, ?, ?, ?)",
            (ts, category, action, json.dumps(properties), session_id, project_id),
        )


def _T(year: int = 2026, month: int = 5, day: int = 1, hour: int = 0,
       minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)




def test_funnel_counts_in_order(tmp_db):
    _seed([
        (_T(day=1, hour=1), "workflow", "start", {}, "A", "corsia"),
        (_T(day=1, hour=2), "spec", "approved", {}, "A", "corsia"),
        (_T(day=1, hour=3), "workflow", "completed", {}, "A", "corsia"),
        (_T(day=2, hour=1), "workflow", "start", {}, "B", "corsia"),
        (_T(day=2, hour=2), "spec", "approved", {}, "B", "corsia"),
        (_T(day=3, hour=1), "workflow", "start", {}, "C", "corsia"),
        (_T(day=4, hour=1), "workflow", "completed", {}, "D", "corsia"),
    ])
    out = analytics.funnel(
        steps=["workflow.start", "spec.approved", "workflow.completed"],
        since=None,
    )
    assert out == {
        "workflow.start": 3,      # A, B, C
        "spec.approved": 2,       # A, B
        "workflow.completed": 1,  # A only (D didn't reach step 1, dropped)
    }


def test_funnel_respects_since_window(tmp_db):
    old = datetime.now(timezone.utc) - timedelta(days=60)
    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    _seed([
        (old, "workflow", "start", {}, "old-sess", "corsia"),
        (fresh, "workflow", "start", {}, "fresh-sess", "corsia"),
    ])
    out = analytics.funnel(["workflow.start"], since=timedelta(days=30))
    assert out == {"workflow.start": 1}


def test_funnel_empty_steps_returns_empty(tmp_db):
    assert analytics.funnel([], since=None) == {}




def test_cohort_compare_groups_and_stats(tmp_db):
    _seed([
        (_T(day=1), "turn", "completed", {"model": "haiku", "duration_ms": 100},
         "s1", "corsia"),
        (_T(day=1), "turn", "completed", {"model": "haiku", "duration_ms": 200},
         "s2", "corsia"),
        (_T(day=1), "turn", "completed", {"model": "haiku", "duration_ms": 300},
         "s3", "corsia"),
        (_T(day=1), "turn", "completed", {"model": "opus", "duration_ms": 500},
         "s4", "corsia"),
        (_T(day=1), "turn", "completed", {"model": "opus", "duration_ms": 700},
         "s5", "corsia"),
    ])
    out = analytics.cohort_compare(
        group_by="properties->>'model'",
        metric="properties->>'duration_ms'",
        since=None,
        filters={"category": "turn", "action": "completed"},
    )
    assert set(out.keys()) == {"haiku", "opus"}
    assert out["haiku"]["n"] == 3
    assert out["haiku"]["mean"] == pytest.approx(200.0)
    assert out["haiku"]["median"] == pytest.approx(200.0)
    assert out["opus"]["n"] == 2
    assert out["opus"]["mean"] == pytest.approx(600.0)




def test_before_after_distribution_shift(tmp_db):
    cutoff = _T(day=15)
    rows = []
    for i, v in enumerate([95, 98, 100, 102, 105]):
        rows.append((_T(day=10, hour=i), "turn", "completed",
                     {"duration_ms": v}, f"b{i}", "corsia"))
    for i, v in enumerate([195, 198, 200, 202, 205]):
        rows.append((_T(day=20, hour=i), "turn", "completed",
                     {"duration_ms": v}, f"a{i}", "corsia"))
    _seed(rows)
    out = analytics.before_after(
        event_filter={"category": "turn", "action": "completed"},
        cutoff_ts=cutoff,
        metric="properties->>'duration_ms'",
    )
    assert out["before"]["n"] == 5
    assert out["after"]["n"] == 5
    assert out["before"]["mean"] == pytest.approx(100.0)
    assert out["after"]["mean"] == pytest.approx(200.0)
    assert out["ttest"]["t"] is not None
    assert out["ttest"]["p"] is not None
    assert out["ttest"]["p"] < 0.01
    assert out["ttest"]["effect_size"] is not None


def test_before_after_handles_empty_sides(tmp_db):
    _seed([
        (_T(day=20), "turn", "completed", {"duration_ms": 100}, "s1", "corsia"),
    ])
    out = analytics.before_after(
        event_filter={"category": "turn", "action": "completed"},
        cutoff_ts=_T(day=15),
        metric="properties->>'duration_ms'",
    )
    assert out["before"]["n"] == 0
    assert out["after"]["n"] == 1
    assert out["ttest"]["t"] is None  # undefined for n<2 on either side




def test_time_series_daily_count(tmp_db):
    _seed([
        (_T(day=1, hour=12), "turn", "started", {}, "s1", "corsia"),
        (_T(day=2, hour=12), "turn", "started", {}, "s2", "corsia"),
        (_T(day=3, hour=12), "turn", "started", {}, "s3", "corsia"),
    ])
    out = analytics.time_series(
        metric="*",
        bucket="day",
        since=None,
        filters={"category": "turn", "action": "started"},
        agg="count",
    )
    values = [v for _ts, v in out]
    assert sum(values) == 3.0  # all 3 events landed somewhere
    assert all(v == 1.0 for v in values)  # one per day → one per bucket
    assert len(out) == 3
    timestamps = [t for t, _v in out]
    assert timestamps == sorted(timestamps)


def test_time_series_avg(tmp_db):
    _seed([
        (_T(day=1, hour=1), "turn", "completed", {"duration_ms": 100},
         "s1", "corsia"),
        (_T(day=1, hour=2), "turn", "completed", {"duration_ms": 200},
         "s2", "corsia"),
    ])
    out = analytics.time_series(
        metric="properties->>'duration_ms'",
        bucket="day",
        since=None,
        filters={"category": "turn", "action": "completed"},
        agg="avg",
    )
    assert len(out) == 1
    assert out[0][1] == pytest.approx(150.0)




def test_distribution_compare_two_models(tmp_db):
    _seed([
        (_T(day=1), "turn", "completed", {"model": "haiku", "duration_ms": 100},
         "h1", "corsia"),
        (_T(day=2), "turn", "completed", {"model": "haiku", "duration_ms": 120},
         "h2", "corsia"),
        (_T(day=3), "turn", "completed", {"model": "haiku", "duration_ms": 130},
         "h3", "corsia"),
        (_T(day=4), "turn", "completed", {"model": "opus", "duration_ms": 800},
         "o1", "corsia"),
        (_T(day=5), "turn", "completed", {"model": "opus", "duration_ms": 850},
         "o2", "corsia"),
        (_T(day=6), "turn", "completed", {"model": "opus", "duration_ms": 900},
         "o3", "corsia"),
    ])
    out = analytics.distribution_compare(
        filter_a={"category": "turn", "model": "haiku"},
        filter_b={"category": "turn", "model": "opus"},
        metric="properties->>'duration_ms'",
    )
    assert out["a"]["n"] == 3
    assert out["b"]["n"] == 3
    assert out["a"]["mean"] == pytest.approx(116.6666, abs=0.1)
    assert out["b"]["mean"] == pytest.approx(850.0)
    assert out["ttest"]["t"] is not None
    assert out["ttest"]["effect_size"] is not None




def test_top_n_count_leaderboard(tmp_db):
    _seed([
        (_T(day=1), "turn", "completed", {}, "s1", "corsia"),
        (_T(day=1), "turn", "completed", {}, "s2", "corsia"),
        (_T(day=1), "turn", "completed", {}, "s3", "corsia"),
        (_T(day=1), "model", "switched", {}, "s4", "corsia"),
        (_T(day=1), "session", "started", {}, "s5", "corsia"),
        (_T(day=1), "session", "started", {}, "s6", "corsia"),
    ])
    out = analytics.top_n(
        group_by="category",
        metric="*",
        n=10,
        since=None,
    )
    assert out[0] == ("turn", 3.0)
    assert out[1] == ("session", 2.0)
    assert out[2] == ("model", 1.0)


def test_top_n_respects_limit(tmp_db):
    _seed([
        (_T(day=1), "turn", "completed", {}, "s1", "corsia"),
        (_T(day=1), "model", "switched", {}, "s2", "corsia"),
        (_T(day=1), "session", "started", {}, "s3", "corsia"),
    ])
    assert len(analytics.top_n("category", "*", n=2, since=None)) == 2




def test_roundtrip_emit_then_query(tmp_db, monkeypatch):
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    product_events.emit_product_event(
        "turn", "completed",
        thread_id="round-trip-sess",
        properties={"model": "haiku", "duration_ms": 250},
    )
    leaderboard = analytics.top_n(
        group_by="category", metric="*", since=None,
    )
    assert leaderboard[0] == ("turn", 1.0)

    stats = analytics.cohort_compare(
        group_by="properties->>'model'",
        metric="properties->>'duration_ms'",
        since=None,
        filters={"category": "turn", "action": "completed"},
    )
    assert stats["haiku"]["n"] == 1
    assert stats["haiku"]["mean"] == pytest.approx(250.0)
