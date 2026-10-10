"""Models reads the page grain's latest window and the one before it, in the viewer's zone."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.test_measurement_api import USER, _freeze_clock, _seed_thread
from tests.plan_writer_authorizer import raw_plan_seed

MODEL = "claude-code/claude-opus-5-5"


def _seed_tasks(instants: list[str]) -> None:
    """One accepted, priced task per instant, settled and charged at that UTC instant."""
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES ('plan','thread','root','active',1,'t','t')")
        for index, accepted_at in enumerate(instants):
            node = f"task-{index}"
            with raw_plan_seed(db):
                db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at) VALUES (?, 'plan',NULL,1,'task','task','done','accepted',?,1,1,'t','t')", (node, node))
                db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) VALUES (?,?,'test','accepted','active','accepted',?)", (f"accepted-{node}", node, accepted_at))
            db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at,plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) VALUES (?, 'root',?,1000000,0,0,0,1000000,'external_harness:claude-code',?,'plan',?,'attempt',?,'execution')", (f"usage-{node}", MODEL, accepted_at, node, node))
        db.commit()
    finally:
        db.close()


async def _row(grain: str, zone: str) -> dict:
    from api.measurement import measurement_models

    served = await measurement_models(project="all", agent="none", grain=grain, timezone_name=zone, user=USER)
    row = next((row for row in served["rows"] if row["model"] == MODEL), None)
    return {"period": served["period"], "row": row}


@pytest.mark.asyncio
async def test_the_days_grain_reads_the_latest_seven_days_and_the_seven_before(isolated_db, monkeypatch):
    _freeze_clock(monkeypatch, datetime(2026, 10, 7, 12, tzinfo=timezone.utc))
    # Latest: Sep 30 to Oct 6. Previous: Sep 23 to Sep 29. Oct 7 is in progress, Sep 22 is older.
    _seed_tasks([f"2026-10-0{1 + i % 5}T12:00:00Z" for i in range(14)]
                + [f"2026-09-2{3 + i % 6}T12:00:00Z" for i in range(5)]
                + ["2026-10-07T01:00:00Z", "2026-09-22T12:00:00Z"])
    served = await _row("days", "UTC")
    assert served["period"]["latest"] == {"from": "2026-09-30", "to": "2026-10-06"}
    assert served["period"]["previous"] == {"from": "2026-09-23", "to": "2026-09-29"}
    assert (served["row"]["tasks"], served["row"]["previous_tasks"]) == (14, 5)


@pytest.mark.asyncio
async def test_the_periods_grain_reads_the_latest_four_periods(isolated_db, monkeypatch):
    _freeze_clock(monkeypatch, datetime(2026, 10, 7, 12, tzinfo=timezone.utc))
    # Latest: Sep 7 to Oct 5. Previous: Aug 10 to Sep 7.
    _seed_tasks(["2026-09-23T12:00:00Z"] * 14 + ["2026-08-19T12:00:00Z"] * 3 + ["2026-10-06T12:00:00Z"])
    served = await _row("weeks", "UTC")
    assert served["period"]["latest"] == {"from": "2026-09-07", "to": "2026-10-04"}
    assert (served["row"]["tasks"], served["row"]["previous_tasks"]) == (14, 3)


@pytest.mark.parametrize(
    ("now", "zone", "boundary_utc", "before_utc"),
    [
        # US spring-forward (2026-03-08): the latest window's first midnight is PDT (07:00Z).
        (datetime(2026, 3, 17, 12, tzinfo=timezone.utc), "America/Los_Angeles", "2026-03-10T07:00:00Z", "2026-03-10T06:59:59Z"),
        # US fall-back (2026-11-01): the latest window's first midnight is PST (08:00Z).
        (datetime(2026, 11, 10, 12, tzinfo=timezone.utc), "America/Los_Angeles", "2026-11-03T08:00:00Z", "2026-11-03T07:59:59Z"),
        # A half-hour zone: midnight at +05:30 is 18:30Z the evening before.
        (datetime(2026, 10, 7, 12, tzinfo=timezone.utc), "Asia/Kolkata", "2026-09-29T18:30:00Z", "2026-09-29T18:29:59Z"),
    ],
)
@pytest.mark.asyncio
async def test_a_day_window_starts_at_the_viewers_local_midnight(isolated_db, monkeypatch, now, zone, boundary_utc, before_utc):
    del before_utc
    _freeze_clock(monkeypatch, now)
    latest_noon = f"{(now.date().replace(day=now.day - 3)).isoformat()}T12:00:00Z"
    _seed_tasks([latest_noon] * 14 + [boundary_utc])
    served = await _row("days", zone)
    assert served["period"]["timezone"] == zone
    assert (served["row"]["tasks"], served["row"]["previous_tasks"]) == (15, 0)


@pytest.mark.parametrize(
    ("now", "zone", "before_utc"),
    [
        (datetime(2026, 3, 17, 12, tzinfo=timezone.utc), "America/Los_Angeles", "2026-03-10T06:59:59Z"),
        (datetime(2026, 11, 10, 12, tzinfo=timezone.utc), "America/Los_Angeles", "2026-11-03T07:59:59Z"),
        (datetime(2026, 10, 7, 12, tzinfo=timezone.utc), "Asia/Kolkata", "2026-09-29T18:29:59Z"),
    ],
)
@pytest.mark.asyncio
async def test_the_last_second_before_local_midnight_belongs_to_the_previous_window(isolated_db, monkeypatch, now, zone, before_utc):
    _freeze_clock(monkeypatch, now)
    latest_noon = f"{(now.date().replace(day=now.day - 3)).isoformat()}T12:00:00Z"
    _seed_tasks([latest_noon] * 14 + [before_utc])
    served = await _row("days", zone)
    assert (served["row"]["tasks"], served["row"]["previous_tasks"]) == (14, 1)
