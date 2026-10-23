"""Data-backed starts and comparability boundaries for Measurement."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from api import measurement
from api import measurement_breaks
from api.measurement import _row
from api.measurement_breaks import derive_breaks, period_comparable, record_source_starts, source_started_ats
from core.measurement_scope import resolve_grain
from infra.database import get_db
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_measurement_api import _seed_decision, _seed_outcomes, _seed_thread


def _grain():
    return resolve_grain("weeks", "UTC", now=datetime(2026, 10, 22, tzinfo=timezone.utc))


class _Months:
    zone = "UTC"

    @staticmethod
    def period_of(day: str) -> str:
        return day[:7] + "-01"


def _thread(db) -> None:
    db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('thread','steve','t','t','t')")
    db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES ('plan','thread','thread','active',1,'t','t')")


def _legacy_mark(db, period: str, kind: str, metric_group: str | None, reason: str) -> None:
    """Add history produced only when this test models a pre-v144 installation."""
    db.execute(
        "INSERT INTO measure_breaks(period,kind,metric_group,reason) VALUES (?,?,?,?)",
        (period, kind, metric_group, reason),
    )


def test_derived_starts_and_world_break_come_from_rows_and_table(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        with raw_plan_seed(db):
            db.execute("INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at) VALUES ('node','plan',0,'task','t','t','accepted','c',1,1,'t','t')")
            db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) VALUES ('event','node','test','accepted','active','accepted','2026-08-10T12:00:00Z')")
        _seed_decision(db, 1, None, raised="2026-08-10T12:00:00Z", thread_id="thread")
        _legacy_mark(db, "2026-08-31", "world", None, "plan nodes got about 5 times smaller")
        db.execute("INSERT INTO plan_node_holds (id,node_id,kind,reason_key,reason,owner_kind,provenance,created_at) VALUES ('hold','node','blocked','test','test','user','test','2026-09-07T12:00:00Z')")
        db.commit()
        breaks = derive_breaks(db, ("outcomes", "asks_per_outcome"), _grain())
    finally:
        db.close()
    assert breaks["outcomes"] == [{"period": "2026-08-10", "kind": "start", "reason": "instrument source first recorded"}]
    assert breaks["asks_per_outcome"] == [
        {"period": "2026-08-10", "kind": "start", "reason": "instrument source first recorded"},
        {"period": "2026-09-07", "kind": "source", "reason": "user-owned holds first recorded"},
        {"period": "2026-08-31", "kind": "world", "reason": "plan nodes got about 5 times smaller"},
    ]
    assert not period_comparable("asks_per_outcome", "2026-08-24", breaks["asks_per_outcome"])
    assert period_comparable("asks_per_outcome", "2026-09-07", breaks["asks_per_outcome"])
    assert _row("outcomes", "2026-09-07", {"outcomes": 1}, False, breaks=breaks["outcomes"])["method_version"] == 1


def test_bulk_lane_discovery_hands_a_start_to_the_write_lane(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at) VALUES ('usage','thread','m',1,0,0,0,1,'test','2026-03-02T12:00:00Z')")
        db.commit()
        pending: dict[str, str | None] = {}
        assert source_started_ats(db, ('fresh_tokens',), pending_starts=pending) == {'tokens': '2026-03-02T12:00:00Z'}
        assert db.execute("SELECT COUNT(*) FROM measure_breaks WHERE kind='start'").fetchone()[0] == 0
        record_source_starts(db, pending)
        assert db.execute("SELECT period FROM measure_breaks WHERE kind='start' AND metric_group='tokens'").fetchone()[0] == '2026-03-02T12:00:00Z'
    finally:
        db.close()


def test_recorded_start_does_not_move_after_its_raw_source_is_deleted(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at) VALUES ('usage','thread','m',1,0,0,0,1,'test','2026-03-02T12:00:00Z')")
        _legacy_mark(db, "2026-09-07", "source", "tokens", "token totals cover every harness from this week")
        db.commit()
        first = derive_breaks(db, ('fresh_tokens',), _grain())
        db.execute("DELETE FROM llm_usage WHERE id='usage'")
        db.commit()
        after_deletion = derive_breaks(db, ('fresh_tokens',), _grain())
    finally:
        db.close()

    expected = [
        {"period": "2026-03-02", "kind": "start", "reason": "instrument source first recorded"},
        {"period": "2026-09-07", "kind": "source", "reason": "token totals cover every harness from this week"},
    ]
    assert first["fresh_tokens"] == expected
    assert after_deletion["fresh_tokens"] == expected


def test_recorded_asks_hold_does_not_move_after_its_raw_source_is_deleted(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        _seed_decision(db, 1, None, raised="2026-09-01T12:00:00Z", thread_id="thread")
        db.execute("INSERT INTO plan_node_holds (id,node_id,kind,reason_key,reason,owner_kind,provenance,created_at) VALUES ('hold','node','blocked','test','test','user','test','2026-09-07T12:00:00Z')")
        db.commit()
        first = derive_breaks(db, ("asks_per_outcome",), _grain())
        assert db.execute("SELECT period FROM measure_breaks WHERE kind='source' AND metric_group='asks_hold'").fetchone()[0] == "2026-09-07T12:00:00Z"
        db.execute("DROP TRIGGER prevent_plan_hold_delete")
        db.execute("DELETE FROM plan_node_holds WHERE id='hold'")
        db.commit()
        after_deletion = derive_breaks(db, ("asks_per_outcome",), _grain())
    finally:
        db.close()

    expected = [
        {"period": "2026-08-31", "kind": "start", "reason": "instrument source first recorded"},
        {"period": "2026-09-07", "kind": "source", "reason": "user-owned holds first recorded"},
    ]
    assert first["asks_per_outcome"] == expected
    assert after_deletion["asks_per_outcome"] == expected


def test_first_try_starts_when_the_last_required_miss_signal_appears(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        with raw_plan_seed(db):
            db.execute("INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at) VALUES ('node','plan',0,'task','t','t','accepted','c',1,1,'t','t')")
            db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) VALUES ('accepted','node','test','accepted','active','accepted','2026-08-10T12:00:00Z')")
            db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) VALUES ('rejected','node','test','review_rejected','active','active','2026-09-14T12:00:00Z')")
        db.commit()
        breaks = derive_breaks(db, ("first_try_acceptance",), _grain())
    finally:
        db.close()
    assert breaks["first_try_acceptance"][0] == {
        "period": "2026-09-14", "kind": "start", "reason": "instrument source first recorded",
    }


def test_mark_at_or_before_a_metric_start_is_not_returned(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        _seed_decision(db, 1, None, raised="2026-09-01T12:00:00Z", thread_id="thread")
        _legacy_mark(db, "2026-08-31", "world", None, "plan nodes got about 5 times smaller")
        _legacy_mark(db, "2026-09-01", "source", "asks_per_outcome", "same-day source mark")
        db.commit()
        assert derive_breaks(db, ("asks_per_outcome",), _grain())["asks_per_outcome"] == [
            {"period": "2026-08-31", "kind": "start", "reason": "instrument source first recorded"}
        ]
    finally:
        db.close()


def test_world_break_maps_to_requested_month_grain(isolated_db) -> None:
    db = get_db()
    try:
        _legacy_mark(db, "2026-08-31", "world", None, "plan nodes got about 5 times smaller")
        assert derive_breaks(db, ("asks_per_outcome",), _Months())["asks_per_outcome"] == [
            {"period": "2026-08-01", "kind": "world", "reason": "plan nodes got about 5 times smaller"}
        ]
    finally:
        db.close()


def test_token_completeness_break_keeps_earlier_available_token_periods(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at) VALUES ('usage','thread','m',1,0,0,0,1,'test','2026-03-02T12:00:00Z')")
        _legacy_mark(db, "2026-09-07", "source", "tokens", "token totals cover every harness from this week")
        db.commit()
        breaks = derive_breaks(db, ("fresh_tokens", "limit_hits"), _grain())
    finally:
        db.close()
    assert breaks["fresh_tokens"] == [
        {"period": "2026-03-02", "kind": "start", "reason": "instrument source first recorded"},
        {"period": "2026-09-07", "kind": "source", "reason": "token totals cover every harness from this week"},
    ]
    assert {"period": "2026-09-07", "kind": "source", "reason": "token totals cover every harness from this week"} not in breaks["limit_hits"]


def test_size_model_break_is_requested_lazily_and_uses_rater(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        with raw_plan_seed(db):
            for node_id, settled in (("size-node-one", "2026-09-01T12:00:00Z"), ("size-node-two", "2026-09-15T12:00:00Z")):
                db.execute("INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at) VALUES (?,?,0,'task','t','t','accepted',?,1,1,'t','t')", (node_id, "plan", node_id))
                db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) VALUES (?,?, 'test','accepted','active','accepted',?)", (f"{node_id}-event", node_id, settled))
        for event_id, node_id, rater, observed in (("size-one", "size-node-one", "rater-a", "2026-09-01T12:00:00Z"), ("size-two", "size-node-two", "rater-b", "2026-09-15T12:00:00Z")):
            db.execute("INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,attributes) VALUES (?,?,?,?,?,?,?,?,?)", (event_id, event_id, "measurement.size_rating", "corsia", "agent_recorded", observed, observed, f"plan_node:{node_id}", json.dumps({"rater": rater})))
        db.commit()
        executed: list[str] = []
        db.set_trace_callback(executed.append)
        nonsize = derive_breaks(db, ("outcomes",), _grain())
        sized = derive_breaks(db, ("estimated_human_hours_per_active_hour",), _grain())
        model_query = next(sql for sql in executed if "SELECT json_extract(f.attributes,'$.rater')" in sql)
        model_plan = "\n".join(str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + model_query))
    finally:
        db.close()
    assert nonsize["outcomes"] == [{"period": "2026-08-31", "kind": "start", "reason": "instrument source first recorded"}]
    assert {"period": "2026-09-14", "kind": "model", "reason": "rater changed"} in sized["estimated_human_hours_per_active_hour"]
    assert "SCAN outcome_events" not in model_plan
    assert "USE TEMP B-TREE" not in model_plan
    assert "idx_measurement_size_rater" in model_plan


@pytest.mark.asyncio
async def test_delayed_agent_early_period_is_a_measured_zero(isolated_db) -> None:
    """A later scope cannot move an already-installed instrument's start boundary."""
    db = get_db()
    try:
        _thread(db)
        with raw_plan_seed(db):
            db.execute("INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at) VALUES ('early','plan',0,'task','t','t','accepted','early',1,1,'t','t')")
            db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) VALUES ('early-event','early','test','accepted','active','accepted','2026-08-10T12:00:00Z')")
        _seed_thread(db, "delayed", agent=True)
        _seed_outcomes(db, "delayed", count=1)
        db.commit()
    finally:
        db.close()

    payload = await measurement.measurement_series(
        "outcomes", project="all", agent="delayed", grain="weeks", timezone_name="UTC", user={"id": "steve"},
    )
    early = next(row for row in payload["rows"] if row["series"] == "outcomes" and row["period"] == "2026-08-10")
    assert early["method_version"] == 1
    assert (early["value"], early["raw_value"], early["n"]) == (0, 0, 0)



def test_week_before_instrument_start_is_not_measured() -> None:
    """The row serializer never turns a pre-instrument absence into a zero."""
    before_start = _row("outcomes", "2026-08-03", None, True, grain=_grain(), source_start="2026-08-10")
    assert (before_start["value"], before_start["raw_value"], before_start["n"]) == (None, None, 0)


@pytest.mark.asyncio
async def test_series_index_start_reads_keep_only_the_indexes_needed_after_start_persistence(isolated_db, monkeypatch) -> None:
    """A first discovery may scan; recurring source reads keep only their needed indexes."""
    db = get_db()
    try:
        _thread(db)
        db.commit()
    finally:
        db.close()

    queries: list[str] = []
    original = measurement_breaks._breaks_query

    def traced_query(groups, include_models: bool) -> str:
        query = original(groups, include_models)
        queries.append(query)
        return query

    monkeypatch.setattr(measurement_breaks, "_breaks_query", traced_query)
    await measurement.measurement_index(project="all", agent="none", timezone_name="UTC", user={"id": "steve"})
    db = get_db()
    try:
        plans = [str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + queries[0])]
        index_names = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        db.close()
    plan = "\n".join(plans)
    assert len(queries) == 1
    assert "SCALAR SUBQUERY" in plan
    for table in ("plan_node_events", "plan_nodes", "llm_usage", "outcome_events", "artifact_identities"):
        assert f"SCAN {table}" not in plan
    assert "USE TEMP B-TREE" not in plan
    for index in ("idx_llm_usage_priceable_measurement_start", "idx_measurement_user_holds_start",
                  "idx_measurement_decisions_raised", "idx_measurement_decisions_answered",
                  "idx_measurement_size_rater"):
        assert index in plan
    assert {"idx_measurement_presence_start", "idx_measurement_active_turns_start"}.isdisjoint(index_names)
