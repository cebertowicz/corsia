"""The Measurement key figures: the latest window of completed periods beside the one before, in scope."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.test_measurement_api import USER, _seed_plan, _seed_task, _seed_thread

ZONE = "America/Los_Angeles"


def _pin_now(monkeypatch):
    import api.measurement_recent as measurement

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 8, 20, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(measurement, "datetime", FixedDateTime)
    monkeypatch.setattr("core.measurement_scope._now", lambda: FixedDateTime.now(timezone.utc))
    return measurement


def _usage(db, usage_id, thread, created_at, *, parts=(None, None, None, None), total=None):
    prompt, completion, creation, read = parts
    db.execute(
        "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,"
        "total_tokens,cost_usd,source,created_at) VALUES (?,?,'test',?,?,?,?,?,0,'test',?)",
        (usage_id, thread, prompt, completion, creation, read, total, created_at),
    )


def _seed_scope(db):
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p1','One','t','t')")
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p2','Two','t','t')")
    _seed_thread(db, "root", project_id="p1")
    _seed_thread(db, "other", project_id="p2")
    _seed_plan(db, "plan-root", "root")
    _seed_plan(db, "plan-other", "other")


# Local days in Los Angeles: today is [2026-10-08T07:00Z, 2026-10-09T07:00Z) and still in progress. On the Days grain the
# current window is the seven completed days before it, [2026-10-01T07:00Z, 2026-10-08T07:00Z), and the previous one the
# seven before that.
async def _response(measurement, project, grain="days"):
    return await measurement.measurement_key_figures(project=project, agent="none", timezone_name=ZONE, grain=grain, user=USER)


async def _kpi(measurement, project):
    return (await _response(measurement, project))["kpi"]


@pytest.mark.asyncio
async def test_kpi_compares_the_last_seven_days_with_the_seven_before_and_honors_the_project_scope(isolated_db, monkeypatch):
    measurement = _pin_now(monkeypatch)
    from infra.database import get_db

    db = get_db()
    try:
        _seed_scope(db)
        _seed_task(db, "plan-root", "t1", settled="2026-10-07T10:00:00Z")
        _seed_task(db, "plan-root", "t2", settled="2026-10-07T11:00:00Z")
        _seed_task(db, "plan-root", "y1", settled="2026-09-30T20:00:00Z")
        _seed_task(db, "plan-other", "o1", settled="2026-10-07T10:30:00Z")
        _usage(db, "today", "root", "2026-10-07T12:00:00.000000", parts=(1000, 500, 200, 3000), total=4700)
        _usage(db, "period-before", "root", "2026-09-30T21:00:00.000000", parts=(100, 50, 20, 300), total=470)
        _usage(db, "elsewhere", "other", "2026-10-07T12:00:00.000000", parts=(9_000_000, 1, 1, 1), total=9_000_003)
        db.commit()
    finally:
        db.close()

    response = await _response(measurement, "p1")
    today, yesterday = response["kpi"]["current"], response["kpi"]["previous"]
    assert response["today"] == "2026-10-08" and "days" not in response
    assert (today["from"], today["to"], yesterday["from"], yesterday["to"]) == ("2026-10-01", "2026-10-07", "2026-09-24", "2026-09-30")
    assert "outcomes" not in today and "outcomes" not in yesterday  # the row shows tokens only; no unread count is served
    assert {name: figure["value"] for name, figure in today["tokens"].items()} == {
        "total": 4700, "input": 1000, "cache_write": 200, "cache_read": 3000, "output": 500}
    assert yesterday["tokens"]["total"] == {"value": 470, "unknown_records": 0}

    everything = await _kpi(measurement, "all")
    assert everything["current"]["tokens"]["total"]["value"] == 9_004_703


@pytest.mark.asyncio
async def test_kpi_total_is_fresh_plus_cache_reads_from_the_one_token_definition(isolated_db, monkeypatch):
    from api.measurement_token_sql import FRESH_TOKEN_SQL
    from infra.database import get_db

    measurement = _pin_now(monkeypatch)
    db = get_db()
    try:
        _seed_scope(db)
        # One row with a stored total, one rebuilt from its parts: both must agree with the fresh measure plus cache reads.
        _usage(db, "stored", "root", "2026-10-07T12:00:00.000000", parts=(1000, 500, 200, 3000), total=4700)
        _usage(db, "rebuilt", "root", "2026-10-07T13:00:00.000000", parts=(10, 5, 2, 30))
        fresh = db.execute(f"SELECT SUM({FRESH_TOKEN_SQL}) AS fresh FROM llm_usage u").fetchone()["fresh"]
        db.commit()
    finally:
        db.close()

    tokens = (await _kpi(measurement, "p1"))["current"]["tokens"]
    assert tokens["total"]["value"] == 4700 + 47
    assert tokens["total"]["value"] == fresh + tokens["cache_read"]["value"]
    assert tokens["total"]["unknown_records"] == 0


@pytest.mark.asyncio
async def test_kpi_input_cache_write_cache_read_and_output_add_up_to_the_total(isolated_db, monkeypatch):
    measurement = _pin_now(monkeypatch)
    from infra.database import get_db

    db = get_db()
    try:
        _seed_scope(db)
        # A stored total that is not the sum of its parts, a rebuilt (NULL) total, and two rows with missing parts.
        _usage(db, "stored", "root", "2026-10-07T12:00:00.000000", parts=(1000, 500, 200, 3000), total=5000)
        _usage(db, "rebuilt", "root", "2026-10-07T12:10:00.000000", parts=(70, 11, 9, 400))
        _usage(db, "stored-no-write", "root", "2026-10-07T12:20:00.000000", parts=(None, 40, None, 600), total=800)
        _usage(db, "rebuilt-no-prompt", "root", "2026-10-07T12:30:00.000000", parts=(None, 25, 5, 100))
        db.commit()
    finally:
        db.close()

    tokens = (await _kpi(measurement, "p1"))["current"]["tokens"]
    values = {name: figure["value"] for name, figure in tokens.items()}
    assert values == {"total": 6420, "input": 1530, "cache_write": 214, "cache_read": 4100, "output": 576}
    assert values["input"] + values["cache_write"] + values["cache_read"] + values["output"] == values["total"]
    # Input is unknown where the total is (no prompt, no stored total) or a part it subtracts was not recorded.
    assert {name: figure["unknown_records"] for name, figure in tokens.items()} == {
        "total": 1, "input": 2, "cache_write": 1, "cache_read": 0, "output": 0}


@pytest.mark.asyncio
async def test_kpi_a_period_with_no_calls_is_a_known_zero(isolated_db, monkeypatch):
    measurement = _pin_now(monkeypatch)
    from infra.database import get_db

    db = get_db()
    try:
        _seed_scope(db)
        _seed_task(db, "plan-root", "y1", settled="2026-09-30T20:00:00Z")
        db.commit()
    finally:
        db.close()

    kpi = await _kpi(measurement, "p1")
    zero = {"value": 0, "unknown_records": 0}
    assert all("outcomes" not in period for period in kpi.values())
    for period in kpi.values():
        assert period["tokens"] == {"total": zero, "input": zero, "cache_write": zero, "cache_read": zero, "output": zero}


@pytest.mark.asyncio
async def test_kpi_counts_calls_that_recorded_no_tokens_as_unknown_beside_what_was_recorded(isolated_db, monkeypatch):
    measurement = _pin_now(monkeypatch)
    from infra.database import get_db

    db = get_db()
    try:
        _seed_scope(db)
        _usage(db, "uncounted", "root", "2026-10-07T12:00:00.000000")
        _usage(db, "counted", "root", "2026-10-07T13:00:00.000000", parts=(10, 5, 2, 30), total=47)
        db.commit()
    finally:
        db.close()

    tokens = (await _kpi(measurement, "p1"))["current"]["tokens"]
    # The recorded call is summed; the call with no counts is flagged, never summed as a hidden zero.
    assert tokens["total"] == {"value": 47, "unknown_records": 1}
    assert tokens["output"] == {"value": 5, "unknown_records": 1}


@pytest.mark.asyncio
async def test_kpi_assigns_boundary_instants_to_the_right_local_window(isolated_db, monkeypatch):
    measurement = _pin_now(monkeypatch)
    from infra.database import get_db

    db = get_db()
    try:
        _seed_scope(db)
        # Stored timestamps are naive UTC with microseconds. Los Angeles midnight is 07:00 UTC.
        for usage_id, at in [("last-of-previous", "2026-10-01T06:59:59.999999"), ("first-of-current", "2026-10-01T07:00:00.000000"),
                             ("before-previous", "2026-09-24T06:59:59.999999"), ("first-of-previous", "2026-09-24T07:00:00.000000"),
                             ("first-of-today", "2026-10-08T07:00:00.000000")]:
            _usage(db, usage_id, "root", at, parts=(1, 1, 1, 1), total=4)
        db.commit()
    finally:
        db.close()

    kpi = await _kpi(measurement, "p1")
    assert kpi["current"]["tokens"]["total"]["value"] == 4, "only the call at the window's first instant is in it, and today's call is in neither"
    assert kpi["previous"]["tokens"]["total"]["value"] == 8, "the previous window ends where the current one starts"


@pytest.mark.asyncio
async def test_kpi_says_whether_a_window_began_after_the_tokens_were_fully_recorded(isolated_db, monkeypatch):
    measurement = _pin_now(monkeypatch)
    from infra.database import get_db

    db = get_db()
    try:
        _seed_scope(db)
        _usage(db, "before-recording", "root", "2026-08-25T21:00:00.000000", parts=(100, 50, 20, 300), total=470)
        db.commit()
    finally:
        db.close()

    weeks = (await _response(measurement, "p1", grain="weeks"))["kpi"]
    days = (await _response(measurement, "p1", grain="days"))["kpi"]
    # The four weeks before 2026-09-07 began 2026-08-10, so their tokens are only partly recorded and their sum is not a base.
    assert (weeks["current"]["tokens_recorded"], weeks["previous"]["tokens_recorded"]) == (True, False)
    assert (days["current"]["tokens_recorded"], days["previous"]["tokens_recorded"]) == (True, True)


@pytest.mark.asyncio
async def test_kpi_token_completeness_uses_a_fresh_installations_first_recorded_day(isolated_db, monkeypatch):
    measurement = _pin_now(monkeypatch)
    from infra.database import get_db

    db = get_db()
    try:
        _seed_scope(db)
        _usage(db, "first-recorded", "root", "2026-08-01T12:00:00.000000", parts=(1, 1, 1, 1), total=4)
        db.commit()
    finally:
        db.close()

    weeks = (await _response(measurement, "p1", grain="weeks"))["kpi"]
    assert (weeks["current"]["tokens_recorded"], weeks["previous"]["tokens_recorded"]) == (True, True)


@pytest.mark.asyncio
async def test_kpi_token_completeness_starts_when_an_upgrade_begins_persisting_complete_rows(isolated_db, monkeypatch):
    measurement = _pin_now(monkeypatch)
    from infra.database import get_db

    db = get_db()
    try:
        _seed_scope(db)
        _usage(db, "old-incomplete", "root", "2026-08-01T12:00:00.000000")
        _usage(db, "first-complete", "root", "2026-09-07T12:00:00.000000", parts=(1, 1, 1, 1), total=4)
        db.commit()
    finally:
        db.close()

    weeks = (await _response(measurement, "p1", grain="weeks"))["kpi"]
    assert (weeks["current"]["tokens_recorded"], weeks["previous"]["tokens_recorded"]) == (True, False)


def test_tokens_recorded_from_memoizes_the_first_complete_day_per_database():
    from api.measurement_kpi import _tokens_recorded_from

    class Db:
        def __init__(self):
            self.calls = 0

        def execute(self, _query):
            self.calls += 1
            return self

        def fetchone(self):
            return {"day": "2026-09-07"}

    db = Db()

    assert _tokens_recorded_from(db) == "2026-09-07"
    assert _tokens_recorded_from(db) == "2026-09-07"
    assert db.calls == 1


@pytest.mark.asyncio
async def test_kpi_on_the_periods_grain_compares_the_last_four_completed_weeks_with_the_four_before(isolated_db, monkeypatch):
    measurement = _pin_now(monkeypatch)
    from infra.database import get_db

    db = get_db()
    try:
        _seed_scope(db)
        # Local weeks in Los Angeles run Monday 00:00 PDT (07:00Z). The four completed weeks began 2026-09-07T07:00Z and end
        # where the period in progress starts (2026-10-05T07:00Z); the four before them began 2026-08-10T07:00Z.
        _seed_task(db, "plan-root", "in-progress-period", settled="2026-10-07T10:00:00Z")
        _seed_task(db, "plan-root", "last-of-window", settled="2026-10-05T06:59:59Z")
        _seed_task(db, "plan-root", "window-first", settled="2026-09-07T07:00:00Z")
        _seed_task(db, "plan-root", "just-before", settled="2026-09-07T06:00:00Z")
        _seed_task(db, "plan-root", "earlier", settled="2026-08-20T12:00:00Z")
        _seed_task(db, "plan-root", "too-early", settled="2026-08-10T06:00:00Z")
        _usage(db, "this-period-so-far", "root", "2026-10-06T12:00:00.000000", parts=(9, 9, 9, 9), total=36)
        _usage(db, "completed-periods", "root", "2026-09-20T12:00:00.000000", parts=(1000, 500, 200, 3000), total=4700)
        _usage(db, "four-periods-back", "root", "2026-08-25T21:00:00.000000", parts=(100, 50, 20, 300), total=470)
        db.commit()
    finally:
        db.close()

    response = await _response(measurement, "p1", grain="weeks")
    period, before = response["kpi"]["current"], response["kpi"]["previous"]
    assert response["period"]["grain"] == "weeks" and response["period"]["current"] == "2026-10-05"
    assert (period["from"], period["to"], before["from"], before["to"]) == ("2026-09-07", "2026-10-04", "2026-08-10", "2026-09-06")
    assert "outcomes" not in period and "outcomes" not in before
    assert period["tokens"]["total"] == {"value": 4700, "unknown_records": 0}
    assert before["tokens"]["total"] == {"value": 470, "unknown_records": 0}
