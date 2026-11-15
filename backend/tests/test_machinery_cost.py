"""Machinery facts through the real daily producer and series route."""
import json
from datetime import datetime, timezone

import pytest

from core.machinery_cost import CATEGORIES, DAY_EVENT, RECORD_EVENT
from core.thread_lifecycle import LifecycleConfig
from infra.database import get_db
from jobs.machinery_cost import TOOL_ROWS_SQL, run_daily
from tests.test_measurement_api import USER, WEEK, _freeze_clock, _seed_outcomes, _seed_thread
from tools.catalog import TOOL_CATALOG


def test_catalog_and_index_contract(isolated_db):
    from infra.measurement_starts import source_first_row_sql
    db = get_db()
    try:
        assert {entry.category for entry in TOOL_CATALOG.values()} <= CATEGORIES
        plan = [r[3] for r in db.execute("EXPLAIN QUERY PLAN " + TOOL_ROWS_SQL, ("x", "2026-01-01", "2026-01-02"))]
        assert any("SEARCH" in line and "idx_messages_session" in line for line in plan)
        plan = [r[3] for r in db.execute("EXPLAIN QUERY PLAN " + source_first_row_sql("first_machinery_day"))]
        assert any("SEARCH" in line and "idx_outcome_events_name_ts" in line for line in plan)
    finally:
        db.close()


def _tool(db, ident, thread, name, **metadata):
    # Stored metadata shape from core.tool_result_delivery.persist_tool_execution_result.
    db.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at) VALUES (?,?,'tool','abc',?,?)",
               (ident, thread, json.dumps({"name": name, **metadata}), "2026-09-23T12:00:00+00:00"))


@pytest.mark.asyncio
async def test_daily_to_route_and_missing_day(isolated_db, monkeypatch):
    from api.measurement import measurement_series
    db = get_db()
    try:
        _seed_thread(db, "root", agent=True)
        _seed_thread(db, "second", agent=True)
        _seed_thread(db, "chat", agent=True)
        _seed_outcomes(db, "root", count=20)
        _tool(db, "m1", "root", "tool_call", tool_name="plan_update", tool_result_byte_count=3)
        _tool(db, "m2", "root", "read_file", tool_result_byte_count=3)
        _tool(db, "m3", "root", "mystery", tool_result_byte_count=3)
        _tool(db, "m4", "second", "remote", mcp_app="calendar", tool_result_byte_count=3)
        # guidance_loads shape written by core.guidance_loads.record_guidance_load.
        db.execute("INSERT INTO guidance_loads(id,thread_id,path,raw_sha256,git_blob_oid,delivered_sha256,byte_count,partial,created_at) VALUES ('g','root','SKILL.md','a','b','c',6,1,'2026-09-23T12:00:00+00:00')")
        db.execute("INSERT INTO llm_usage(id,thread_id,model,total_tokens,cache_read_tokens,created_at) VALUES ('u','root','m',100,20,'2026-09-23T12:00:00+00:00')")
        # A reply with no tool call: its tokens belong in every token share's total.
        db.execute("INSERT INTO llm_usage(id,thread_id,model,total_tokens,cache_read_tokens,created_at) VALUES ('v','chat','m',100,20,'2026-09-23T12:00:00+00:00')")
        db.commit()
    finally:
        db.close()
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    assert run_daily(now=now) == 28
    assert run_daily(now=now) == 0
    _freeze_clock(monkeypatch)
    ids = "plan_action_pct,skill_read_action_pct,machinery_returned_pct,ungrouped_action_pct,plan_token_pct,plan_tokens_per_outcome"
    async def rows(agent):
        payload = await measurement_series(ids, project="all", agent=agent, grain="weeks", timezone_name="UTC", user=USER)
        return {row["series"]: row for row in payload["rows"] if row["period"] == WEEK}
    all_rows = await rows("none")
    assert all_rows["plan_action_pct"]["raw_value"] == pytest.approx(1 / 4)
    assert all_rows["plan_action_pct"]["n"] == 4
    assert all_rows["plan_action_pct"]["value"] is None
    assert all_rows["skill_read_action_pct"]["raw_value"] == pytest.approx(1 / 4)
    assert all_rows["machinery_returned_pct"]["raw_value"] == pytest.approx(3 / 12)
    assert all_rows["ungrouped_action_pct"]["raw_value"] == pytest.approx(1 / 4)
    assert all_rows["plan_token_pct"]["raw_value"] == pytest.approx(1 / 6)
    assert all_rows["plan_tokens_per_outcome"]["n"] == 20
    assert (await rows("root"))["plan_action_pct"]["raw_value"] == pytest.approx(1 / 3)
    db = get_db()
    try:
        assert db.execute("SELECT COUNT(*) FROM outcome_events WHERE event_name=?", (RECORD_EVENT,)).fetchone()[0] == 3
        db.execute("DELETE FROM outcome_events WHERE dedupe_key=?", (f"{DAY_EVENT}:2026-09-24",))
        db.commit()
    finally:
        db.close()
    missing = await rows("none")
    assert missing["plan_action_pct"]["value"] is None
    assert missing["plan_action_pct"]["raw_value"] is None
    assert missing["plan_action_pct"]["n"] == 0
    assert run_daily(now=now) == 1
    assert (await rows("none"))["plan_action_pct"]["raw_value"] == pytest.approx(1 / 4)


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinary_bytes", [0, 1000])
async def test_masked_whole_read_period(isolated_db, monkeypatch, ordinary_bytes):
    from api.measurement import measurement_series
    db = get_db()
    try:
        _seed_thread(db, "root", agent=True)
        _tool(db, "masked", "root", "read_file", tool_result_byte_count=25)
        if ordinary_bytes:
            _tool(db, "ordinary", "root", "mystery", tool_result_byte_count=ordinary_bytes)
        db.execute("INSERT INTO guidance_loads(id,thread_id,path,raw_sha256,git_blob_oid,delivered_sha256,byte_count,partial,created_at) VALUES ('whole','root','SKILL.md','a','b','c',408,0,'2026-09-23T12:00:00+00:00')")
        db.commit()
    finally:
        db.close()
    assert run_daily(now=datetime(2026, 9, 28, 12, tzinfo=timezone.utc)) == 28
    _freeze_clock(monkeypatch)
    ids = "skill_read_returned_pct,machinery_returned_pct,skill_read_action_pct,machinery_action_pct"
    payload = await measurement_series(ids, project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    rows = {row["series"]: row for row in payload["rows"] if row["period"] == WEEK}
    for name in ("skill_read_returned_pct", "machinery_returned_pct"):
        if ordinary_bytes:
            assert rows[name]["raw_value"] == pytest.approx(408 / (25 + ordinary_bytes))
            assert 0 <= rows[name]["raw_value"] <= 1
        else:
            assert (rows[name]["value"], rows[name]["raw_value"], rows[name]["n"]) == (None, None, 0)
    for name in ("skill_read_action_pct", "machinery_action_pct"):
        assert rows[name]["raw_value"] == pytest.approx(1 / (2 if ordinary_bytes else 1))


@pytest.mark.asyncio
async def test_partial_and_whole_skill_reads(isolated_db, monkeypatch):
    from api.measurement import measurement_series
    db = get_db()
    try:
        _seed_thread(db, "root", agent=True)
        _tool(db, "partial", "root", "read_file", tool_result_byte_count=1000)
        _tool(db, "whole", "root", "read_file", tool_result_byte_count=100)
        for ident, thread, size, partial in (("g1", "root", 10000, 1), ("g2", "root", 80, 0), ("g3", None, 5000, 0)):
            db.execute("INSERT INTO guidance_loads(id,thread_id,path,raw_sha256,git_blob_oid,delivered_sha256,byte_count,partial,created_at) VALUES (?, ?, 'SKILL.md', 'a', 'b', 'c', ?, ?, '2026-09-23T12:00:00+00:00')", (ident, thread, size, partial))
        db.commit()
    finally:
        db.close()
    from loguru import logger
    messages = []
    sink = logger.add(messages.append, format="{message}")
    try:
        assert run_daily(now=datetime(2026, 9, 28, 12, tzinfo=timezone.utc)) == 28
    finally:
        logger.remove(sink)
    assert any("skill reads=2 partial=1 whole=1 guidance rows without thread=1" in str(message) for message in messages)
    _freeze_clock(monkeypatch)
    payload = await measurement_series("skill_read_action_pct,skill_read_returned_pct,machinery_returned_pct", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    rows = {row["series"]: row for row in payload["rows"] if row["period"] == WEEK}
    assert rows["skill_read_action_pct"]["raw_value"] == 1
    for name in ("skill_read_returned_pct", "machinery_returned_pct"):
        assert rows[name]["raw_value"] == pytest.approx(80 / 1100)


@pytest.mark.asyncio
async def test_row_without_stored_size_counts_its_content(isolated_db, monkeypatch):
    from api.measurement import measurement_series
    from loguru import logger
    db = get_db()
    try:
        _seed_thread(db, "root", agent=True)
        _tool(db, "sized", "root", "plan_update", tool_result_byte_count=9)
        _tool(db, "unsized", "root", "mystery")
        db.commit()
    finally:
        db.close()
    messages = []
    sink = logger.add(messages.append, format="{message}")
    try:
        assert run_daily(now=datetime(2026, 9, 28, 12, tzinfo=timezone.utc)) == 28
    finally:
        logger.remove(sink)
    assert any("rows without a stored byte count: 1 (first: ['unsized'])" in str(message) for message in messages)
    _freeze_clock(monkeypatch)
    payload = await measurement_series("plan_returned_pct", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    row = next(row for row in payload["rows"] if row["period"] == WEEK)
    assert row["raw_value"] == pytest.approx(9 / 12)


@pytest.mark.asyncio
async def test_skipped_night_is_caught_up_before_week_is_measured(isolated_db, monkeypatch):
    from api.measurement import measurement_series
    db = get_db()
    try:
        _seed_thread(db, "root", agent=True)
        _tool(db, "one", "root", "plan_update", tool_result_byte_count=3)
        db.commit()
    finally:
        db.close()
    assert run_daily(now=datetime(2026, 9, 26, 12, tzinfo=timezone.utc)) == 28
    _freeze_clock(monkeypatch)

    async def week():
        result = await measurement_series("plan_action_pct", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
        return next(row for row in result["rows"] if row["period"] == WEEK)

    assert (await week())["raw_value"] is None
    assert run_daily(now=datetime(2026, 9, 28, 12, tzinfo=timezone.utc)) == 2
    assert (await week())["raw_value"] == 1


@pytest.mark.asyncio
async def test_failed_producer_does_not_change_nightly_terminal(isolated_db, monkeypatch):
    from core import thread_lifecycle
    from jobs import machinery_cost, nightly

    monkeypatch.setattr(thread_lifecycle, "run_lifecycle_retention", lambda: None)
    def fail():
        raise RuntimeError("producer failure")
    monkeypatch.setattr(machinery_cost, "run_daily", fail)
    await nightly._run_job_safely()
    db = get_db()
    try:
        assert db.execute("SELECT status FROM fact_extraction_runs WHERE run_date=?", (datetime.now(timezone.utc).date().isoformat(),)).fetchone()[0] == "ok"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_failed_sweep_still_records_machinery(isolated_db, monkeypatch):
    from core import thread_lifecycle
    from jobs import nightly
    def fail():
        raise RuntimeError("sweep failure")
    monkeypatch.setattr(thread_lifecycle, "run_lifecycle_retention", fail)
    await nightly._run_job_safely()
    db = get_db()
    try:
        assert db.execute("SELECT status FROM fact_extraction_runs WHERE run_date=?", (datetime.now(timezone.utc).date().isoformat(),)).fetchone()[0] == "error"
        assert db.execute("SELECT 1 FROM outcome_events WHERE event_name=?", (DAY_EVENT,)).fetchone()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_retention_bound_and_period_before_first_marker(isolated_db, monkeypatch):
    from api.measurement import measurement_series
    db = get_db()
    try:
        _seed_thread(db, "root", agent=True)
        _tool(db, "one", "root", "plan_update", tool_result_byte_count=3)
        db.commit()
    finally:
        db.close()
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    _freeze_clock(monkeypatch)

    async def periods():
        payload = await measurement_series("plan_action_pct", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
        return {row["period"]: (row["value"], row["raw_value"], row["n"]) for row in payload["rows"]}

    # The first marker is Sep 24: the week of Sep 14 ended before the reading existed,
    # so the route serves no row for it, and the first week is short of days.
    assert run_daily(now=now, config=LifecycleConfig(tool_trim_after_days=5)) == 4
    first = await periods()
    assert min(first) == WEEK
    assert first[WEEK] == (None, None, 0)
    assert run_daily(now=now, config=LifecycleConfig(tool_trim_after_days=10)) == 5
    db = get_db()
    try:
        assert not db.execute("SELECT 1 FROM outcome_events WHERE dedupe_key=?", (f"{DAY_EVENT}:2026-09-16",)).fetchone()
    finally:
        db.close()
    assert (await periods())[WEEK] == (None, 1.0, 1)
