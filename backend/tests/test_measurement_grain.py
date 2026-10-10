"""The page grain: viewer-local days and Monday-to-Sunday weeks, as half-open UTC instants."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from api.measurement_scope import resolve_grain

# Friday 2026-10-09 01:30 in Los Angeles: UTC is already past midnight's offset, so the
# viewer's day, the UTC day and the periods they sit in can all disagree.
NOW = datetime(2026, 10, 9, 8, 30, tzinfo=timezone.utc)
PACIFIC = "America/Los_Angeles"


def test_days_are_the_last_fourteen_completed_local_days_with_today_in_progress():
    grain = resolve_grain("days", PACIFIC, now=NOW)
    assert grain.current == "2026-10-09"
    assert grain.periods[0] == "2026-09-25" and grain.periods[-1] == "2026-10-08" and len(grain.periods) == 14
    assert grain.latest.periods == grain.periods[7:] and grain.previous.periods == grain.periods[:7]


def test_periods_are_the_last_twelve_completed_local_mondays_with_four_against_four():
    grain = resolve_grain("weeks", PACIFIC, now=NOW)
    assert grain.current == "2026-10-05"
    assert grain.periods[0] == "2026-07-13" and grain.periods[-1] == "2026-09-28" and len(grain.periods) == 12
    assert grain.latest.first == "2026-09-07" and grain.latest.last == "2026-09-28"
    assert grain.previous.first == "2026-08-10" and grain.previous.last == "2026-08-31"
    assert grain.describe()["latest"] == {"from": "2026-09-07", "to": "2026-10-04"}


def test_the_viewer_zone_decides_the_day_not_utc():
    utc = resolve_grain("days", "UTC", now=NOW)
    assert utc.current == "2026-10-09" and utc.starts[-1] == "2026-10-09 00"
    pacific = resolve_grain("days", PACIFIC, now=NOW)
    assert pacific.starts[-1] == "2026-10-09 07"
    earlier = resolve_grain("days", PACIFIC, now=datetime(2026, 10, 9, 6, 59, tzinfo=timezone.utc))
    assert earlier.current == "2026-10-08"


def test_a_day_across_a_dst_change_is_twenty_five_hours_long():
    grain = resolve_grain("days", PACIFIC, now=datetime(2026, 11, 3, 12, tzinfo=timezone.utc))
    index = grain.periods.index("2026-11-01")
    assert (grain.starts[index], grain.starts[index + 1]) == ("2026-11-01 07", "2026-11-02 08")


def _bucket(grain, stamp: str, **options):
    db = sqlite3.connect(":memory:")
    return db.execute(f"SELECT {grain.bucket(':ts', **options)}", {**grain.binds(), "ts": stamp}).fetchone()[0]


def test_bucket_names_the_local_period_for_both_stored_timestamp_forms():
    grain = resolve_grain("days", PACIFIC, now=NOW)
    for separator in ("T", " "):
        assert _bucket(grain, f"2026-10-05{separator}06:59:59.999999") == "2026-10-04"
        assert _bucket(grain, f"2026-10-05{separator}07:00:00.000000") == "2026-10-05"


def test_bucket_is_null_outside_the_completed_range_and_the_partial_period_is_opt_in():
    grain = resolve_grain("days", PACIFIC, now=NOW)
    assert _bucket(grain, "2026-09-25T06:59:59") is None
    assert _bucket(grain, "2026-10-09T07:00:00") is None
    assert _bucket(grain, "2026-10-09T07:00:00", partial=True) == "2026-10-09"
    assert _bucket(grain, "2026-10-10T07:00:00", partial=True) is None


def test_weekly_bucket_uses_the_local_monday():
    grain = resolve_grain("weeks", PACIFIC, now=NOW)
    assert _bucket(grain, "2026-09-28T06:59:00") == "2026-09-21"
    assert _bucket(grain, "2026-09-28T07:00:00") == "2026-09-28"


def test_a_zone_with_a_half_hour_offset_buckets_on_the_exact_minute():
    grain = resolve_grain("days", "Asia/Kolkata", now=NOW)
    assert grain.precision == 16
    assert _bucket(grain, "2026-10-05T18:29:59") == "2026-10-05"
    assert _bucket(grain, "2026-10-05T18:30:00") == "2026-10-06"


def test_a_whole_hour_zone_compares_at_hour_precision():
    assert resolve_grain("weeks", PACIFIC, now=NOW).precision == 13


def test_a_window_keeps_exactly_its_local_instants_for_either_separator():
    grain = resolve_grain("days", PACIFIC, now=NOW)
    assert grain.instants(grain.latest) == ("2026-10-02 07", "2026-10-09 07")
    db = sqlite3.connect(":memory:")
    inside = ["2026-10-02T07:00:00", "2026-10-02 07:00:00", "2026-10-09T06:59:59.9"]
    outside = ["2026-10-02T06:59:59", "2026-10-02 06:59:59", "2026-10-09T07:00:00", "2026-10-09 07:00:00"]
    sql = f"SELECT {grain.window_sql(':ts', grain.latest)}"
    assert all(db.execute(sql, {"ts": stamp}).fetchone()[0] for stamp in inside)
    assert not any(db.execute(sql, {"ts": stamp}).fetchone()[0] for stamp in outside)


@pytest.mark.parametrize("kind,zone,status", [("hours", PACIFIC, 400), ("days", "Not/AZone", 422)])
def test_bad_grain_or_zone_is_refused(kind, zone, status):
    with pytest.raises(HTTPException) as caught:
        resolve_grain(kind, zone, now=NOW)
    assert caught.value.status_code == status
