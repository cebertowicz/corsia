"""Every trend row serves its moving average: the measure pooled over the trailing 7 days or 4 weeks."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.test_measurement_cost import CC, SONNET, _client
from tests.test_measurement_windows import NOW, _seed, _seed_source_start, _series


def _reject(day: str, count: int) -> None:
    """``count`` review rejections on the day, on one node that was accepted anyway."""
    from infra.database import get_db

    db = get_db()
    try:
        node = db.execute("SELECT id FROM plan_nodes ORDER BY id LIMIT 1").fetchone()["id"]
        for index in range(count):
            db.execute(
                "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                "VALUES (?,?,'test','review_rejected','review_requested','active',?)", (f"rej-{day}-{index}", node, f"{day}T10:00:00Z"))
        db.commit()
    finally:
        db.close()


def _by_period(served: dict, metric: str) -> dict[str, dict]:
    return {row["period"]: row for row in served["rows"] if row["series"] == metric}


@pytest.mark.asyncio
async def test_days_average_a_rate_pooled_over_seven_days_and_a_count_as_the_span_total_over_seven(isolated_db, monkeypatch):
    monkeypatch.setattr("core.measurement_scope._now", lambda: NOW)
    # The span ending 10-08 holds 30 outcomes (8, 12 and 10), 9 of them rejected: 9 / 30 pooled, where
    # the mean of the busy days' own ratios would be 0.28. The raw starts are installation-wide
    # but this selected scope starts on 10-02, so the earlier source receipt remains out of scope.
    _seed({"2026-10-02": 8, "2026-10-04": 12, "2026-10-08": 10, "2026-10-09": 3})
    _seed_source_start("2026-09-26", rejected=True, rejected_at="2026-09-25")
    _reject("2026-10-04", 3)
    _reject("2026-10-08", 6)

    served = await _series("outcomes,rejections_per_outcome")
    assert served["average_periods"] == 7 and served["period"]["range"]["from"] == "2026-09-25"
    rate, count = _by_period(served, "rejections_per_outcome"), _by_period(served, "outcomes")
    assert rate["2026-10-08"]["average"] == pytest.approx(9 / 30)
    assert rate["2026-10-07"]["average"] == pytest.approx(3 / 20)
    assert count["2026-10-08"]["average"] == pytest.approx(30 / 7)
    assert count["2026-10-07"]["average"] == pytest.approx(20 / 7)
    # A point is drawn only where its whole span lies inside the drawn range: 09-25 is the first drawn day, so
    # no average precedes the first full span, and the scoped outcomes remain outside the source lead-in.
    assert "2026-09-24" not in rate and rate["2026-09-25"]["average"] is None
    assert [day for day in sorted(count) if count[day]["average"] is not None][0] == "2026-10-02"
    assert count["2026-10-02"]["average"] == pytest.approx(8 / 7)
    # Under the series' minimum the average is withheld, as the window is, and never drawn as zero.
    assert rate["2026-10-03"]["average"] is None and count["2026-10-03"]["average"] == pytest.approx(8 / 7)
    # Today so far is not a completed period: it has no average and no span includes it.
    today = [row for row in served["rows"] if row["series"] == "outcomes" and row["partial"]]
    assert [(row["period"], row["average"]) for row in today] == [("2026-10-09", None)]
    assert served["windows"]["rejections_per_outcome"]["latest"]["value"] == pytest.approx(rate["2026-10-08"]["average"])


@pytest.mark.asyncio
async def test_an_average_is_null_while_its_span_reaches_before_the_instrument(isolated_db, monkeypatch):
    # first_try_acceptance is measured from 2026-09-14: the span ending 09-19 starts a day too early, never a zero fill.
    monkeypatch.setattr("core.measurement_scope._now", lambda: datetime(2026, 9, 22, 12, tzinfo=timezone.utc))
    _seed({"2026-09-16": 25})
    _seed_source_start("2026-09-14")

    rows = _by_period(await _series("first_try_acceptance"), "first_try_acceptance")
    assert rows["2026-09-19"]["average"] is None and rows["2026-09-19"]["raw_value"] is None
    assert rows["2026-09-20"]["average"] == 1.0
    assert rows["2026-09-14"]["average"] is None and rows["2026-09-15"]["average"] is None


@pytest.mark.asyncio
async def test_periods_average_over_four_periods_and_skip_the_period_in_progress(isolated_db, monkeypatch):
    monkeypatch.setattr("core.measurement_scope._now", lambda: NOW)
    # Weeks start on Monday; 10-05 is the week in progress. The four weeks ending 09-28 hold 60 outcomes
    # (none in the period of 09-07), 12 of them rejected, over a lead-in that begins before the drawn range.
    _seed({"2026-09-15": 10, "2026-09-22": 20, "2026-09-29": 30, "2026-10-06": 7})
    _seed_source_start("2026-09-07", rejected=True)
    _reject("2026-09-23", 6)
    _reject("2026-09-30", 6)

    served = await _series("outcomes,rejections_per_outcome", grain="weeks")
    assert served["average_periods"] == 4
    # Each series' own registry minimum is served with it, so the page reads one figure for its hollow bars and its change.
    assert served["minimum_n"] == {"outcomes": 0, "rejections_per_outcome": 20}
    rate, count = _by_period(served, "rejections_per_outcome"), _by_period(served, "outcomes")
    assert count["2026-09-28"]["average"] == pytest.approx(60 / 4)
    assert rate["2026-09-28"]["average"] == pytest.approx(12 / 60)
    assert set(rate) == {period for period in rate if period < "2026-10-05"}, "the period in progress is not a rejection row"
    assert [(row["period"], row["average"]) for row in served["rows"] if row["series"] == "outcomes" and row["partial"]] == []


def _seed_cost(days: dict[str, int], spend: dict[str, int]) -> None:
    """Outcomes of class M (2 h each) per day, and 1M-token Sonnet calls (one list-price unit each) per day."""
    from infra.database import get_db

    _seed(days)
    _seed_source_start("2026-09-07")
    db = get_db()
    try:
        db.execute("UPDATE plan_nodes SET size='M'")
        for day, calls in spend.items():
            for index in range(calls):
                db.execute(
                    "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,"
                    "total_tokens,source,created_at) VALUES (?,'root','claude-code/claude-sonnet-5-5',1000000,0,0,0,1000000,?,?)",
                    (f"u-{day}-{index}", CC, f"{day}T12:00:00Z"))
        db.commit()
    finally:
        db.close()


def test_cost_rows_average_the_all_in_cost_per_hour_over_the_span(isolated_db, monkeypatch):
    monkeypatch.setattr("core.measurement_scope._now", lambda: NOW)
    # 3 calls over 24 rated outcomes (48 h) in the span ending 10-08. The spend on 09-19, in the lead-in, starts the series.
    _seed_cost({"2026-10-03": 12, "2026-10-06": 11, "2026-10-08": 1, "2026-09-20": 20}, {"2026-09-19": 1, "2026-09-25": 1, "2026-10-03": 1, "2026-10-06": 2})

    body = _client().get("/api/measurement/cost?grain=days&timezone=UTC&project=all&agent=none").json()
    # The chart names its line by the length the server averaged over, never a client constant.
    assert body["average_periods"] == 7
    rows = {row["period"]: row for row in body["series"]}
    assert body["series"][0]["period"] == "2026-09-25", "the lead-in is read, not drawn"
    assert all(row["period"] >= "2026-09-25" for row in body["series"])
    assert rows["2026-10-08"]["average"] == pytest.approx(3 * SONNET.input_per_million / 48)
    assert rows["2026-10-03"]["average"] is None, "12 rated outcomes are under the minimum"
    # The first drawn day averages over a span reaching into the lead-in, so no point is drawn there, and the
    # spans inside the drawn range before 10-03 hold no rated hours.
    assert all(row["average"] is None for row in body["series"] if row["period"] < "2026-10-03")
    assert body["latest"]["per_hour"] == pytest.approx(rows["2026-10-08"]["average"])


def test_cost_rows_average_four_periods_on_the_periods_grain(isolated_db, monkeypatch):
    monkeypatch.setattr("core.measurement_scope._now", lambda: NOW)
    _seed_cost({"2026-09-15": 10, "2026-09-22": 10, "2026-09-29": 10}, {"2026-09-07": 1, "2026-09-15": 1, "2026-09-29": 2})

    body = _client().get("/api/measurement/cost?grain=weeks&timezone=UTC&project=all&agent=none").json()
    assert body["average_periods"] == 4
    rows = {row["period"]: row for row in body["series"]}
    assert rows["2026-09-28"]["average"] == pytest.approx(4 * SONNET.input_per_million / 60)
    assert rows["2026-09-21"]["average"] is None, "its span starts before the first priced period"


@pytest.mark.asyncio
@pytest.mark.parametrize("grain", ["days", "weeks"])
async def test_every_available_series_serves_an_average_on_every_row(isolated_db, monkeypatch, grain):
    from api.measurement_registry import REGISTRY

    monkeypatch.setattr("core.measurement_scope._now", lambda: NOW)
    _seed({"2026-10-03": 10})
    ids = [name for name, item in REGISTRY.items() if item.available]

    served = await _series(",".join(ids), grain=grain)
    assert served["rows"] and all("average" in row for row in served["rows"])
    assert all(row["average"] is None or isinstance(row["average"], float) for row in served["rows"])
