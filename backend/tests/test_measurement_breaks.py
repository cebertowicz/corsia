"""Data-backed starts and comparability boundaries for Measurement."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from api import measurement
from api.measurement import _row
from api.measurement_breaks import (
    comparability_start, derive_breaks, first_availability_period, period_comparable, source_started_period,
)
from core.measurement_scope import resolve_grain
from infra.database import get_db
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_measurement_api import _seed_decision, _seed_outcomes, _seed_thread


def _grain():
    return resolve_grain("weeks", "UTC", now=datetime(2026, 10, 22, tzinfo=timezone.utc))


def _thread(db) -> None:
    db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('thread','steve','t','t','t')")
    db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES ('plan','thread','thread','active',1,'t','t')")


def _mark(db, period: str, kind: str, metric_group: str | None, reason: str) -> None:
    """Seed an immutable migration fact; live reads never create these rows."""
    db.execute(
        "INSERT INTO measure_breaks(period,kind,metric_group,reason) VALUES (?,?,?,?)",
        (period, kind, metric_group, reason),
    )


def _accepted(db, node_id: str, when: str) -> None:
    with raw_plan_seed(db):
        db.execute(
            "INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at) "
            "VALUES (?,?,0,'task','t','t','accepted',?,1,1,'t','t')",
            (node_id, "plan", node_id),
        )
        db.execute(
            "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
            "VALUES (?,?,'test','accepted','active','accepted',?)",
            (f"{node_id}-accepted", node_id, when),
        )


def _rating(db, node_id: str, rater: str, observed: str = "2026-10-01T12:00:00Z", *, retracted: bool = False) -> None:
    """Write one well-formed size fact for the node's current snapshot."""
    from core.size_rating import current_snapshots

    snap = current_snapshots(db, [node_id])[node_id]
    attributes = {
        "node_id": node_id, "plan_id": snap.plan_id, "node_revision": snap.node_revision,
        "evidence_event_id": snap.evidence_event_id, "source_snapshot_sha256": snap.sha256(),
        "rater_input_sha256": "a" * 64, "method": "offline_proxy", "method_version": "1",
        "rubric_version": "1.3p", "rubric_sha256": "a" * 64, "rater": rater,
        "rater_version": "2026-10-01", "basis": snap.basis, "size_class": "M",
    }
    key = f"{node_id}-{observed}"
    db.execute(
        "INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,attributes,retracted_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (key, key, "measurement.size_rating", "corsia", "agent_recorded", observed, observed, f"plan_node:{node_id}",
         '{"retracted":true}' if retracted else json.dumps(attributes), observed if retracted else None),
    )


def test_fresh_install_live_start_is_primary_and_reads_do_not_write(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        _accepted(db, "node", "2026-08-10T12:00:00Z")
        db.execute(
            "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
            "VALUES ('rejected','node','test','review_rejected','active','active','2026-09-14T12:00:00Z')"
        )
        db.commit()
        before = db.total_changes
        first = derive_breaks(db, ("first_try_acceptance",), _grain())
        second = derive_breaks(db, ("first_try_acceptance",), _grain())
        after = db.total_changes
        rows = db.execute("SELECT COUNT(*) FROM measure_breaks").fetchone()[0]
    finally:
        db.close()
    expected = [{"period": "2026-08-10", "kind": "start", "reason": "first recorded on this computer"}]
    assert first["first_try_acceptance"] == second["first_try_acceptance"] == expected
    assert rows == 0
    assert after == before


def test_legacy_floor_is_used_instead_of_the_live_start(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        _accepted(db, "node", "2026-08-10T12:00:00Z")
        _mark(db, "2026-09-14T12:00:00Z", "start", "first_try", "first recorded on this computer")
        db.execute(
            "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
            "VALUES ('rejected','node','test','review_rejected','active','active','2026-09-14T12:00:00Z')"
        )
        db.commit()
        breaks = derive_breaks(db, ("first_try_acceptance",), _grain())
    finally:
        db.close()
    assert breaks["first_try_acceptance"][0]["period"] == "2026-09-14"


def test_seeded_and_live_starts_share_the_days_viewer_zone_bucket(isolated_db) -> None:
    grain = resolve_grain("days", "America/Los_Angeles", now=datetime(2026, 10, 22, tzinfo=timezone.utc))
    db = get_db()
    try:
        _thread(db)
        _accepted(db, "node", "2026-09-15T00:30:00Z")
        db.commit()
        live = derive_breaks(db, ("outcomes",), grain)
        _mark(db, "2026-09-15T00:30:00Z", "start", "outcomes", "first recorded on this computer")
        db.commit()
        seeded = derive_breaks(db, ("outcomes",), grain)
    finally:
        db.close()
    assert live["outcomes"][0]["period"] == seeded["outcomes"][0]["period"] == "2026-09-14"


def test_size_live_start_requires_a_size_rating_on_an_accepted_outcome(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        _accepted(db, "node", "2026-09-01T12:00:00Z")
        db.commit()
        before_rating = derive_breaks(db, ("estimated_human_hours_per_active_hour",), _grain())
        _rating(db, "node", "rater-a")
        db.commit()
        after_rating = derive_breaks(db, ("estimated_human_hours_per_active_hour",), _grain())
    finally:
        db.close()
    assert before_rating["estimated_human_hours_per_active_hour"] == []
    assert after_rating["estimated_human_hours_per_active_hour"][0]["period"] == "2026-08-31"


def test_legacy_asks_seed_gates_pre_hold_periods_but_fresh_live_start_is_decision(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        _seed_decision(db, 1, None, raised="2026-09-01T12:00:00Z", thread_id="thread")
        db.execute(
            "INSERT INTO plan_node_holds (id,node_id,kind,reason_key,reason,owner_kind,provenance,created_at) "
            "VALUES ('hold','node','blocked','test','test','user','test','2026-09-07T12:00:00Z')"
        )
        _mark(db, "2026-09-07T12:00:00Z", "start", "asks_per_outcome", "first recorded on this computer")
        db.commit()
        legacy = derive_breaks(db, ("asks_per_outcome",), _grain())
        db.execute("DELETE FROM measure_breaks")
        fresh = derive_breaks(db, ("asks_per_outcome",), _grain())
    finally:
        db.close()
    assert legacy["asks_per_outcome"][0]["period"] == "2026-09-07"
    assert fresh["asks_per_outcome"][0]["period"] == "2026-08-31"


def test_model_marks_derive_from_rating_facts_and_are_size_scoped(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        _accepted(db, "baseline", "2026-09-01T12:00:00Z")
        _accepted(db, "new", "2026-09-15T05:30:00Z")
        _rating(db, "baseline", "rater-a")
        _rating(db, "new", "rater-b")
        db.commit()
        sized = derive_breaks(db, ("estimated_human_hours_per_active_hour",), _grain())
        unrelated = derive_breaks(db, ("outcomes", "first_try_acceptance"), _grain())
    finally:
        db.close()
    assert {"period": "2026-09-14", "kind": "model", "reason": "judging model changed"} in sized[
        "estimated_human_hours_per_active_hour"
    ]
    assert not any(mark["kind"] == "model" for marks in unrelated.values() for mark in marks)


def test_model_marks_each_rater_change_and_deduplicates_one_period(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        for node, when, rater in (
            ("baseline", "2026-09-01T12:00:00Z", "rater-a"),
            ("b", "2026-09-15T12:00:00Z", "rater-b"),
            ("a-again", "2026-09-22T12:00:00Z", "rater-a"),
        ):
            _accepted(db, node, when)
            _rating(db, node, rater)
        db.commit()
        marks = [
            mark for mark in derive_breaks(db, ("estimated_human_hours_per_active_hour",), _grain())["estimated_human_hours_per_active_hour"]
            if mark["kind"] == "model"
        ]
    finally:
        db.close()
    assert [mark["period"] for mark in marks] == ["2026-09-14", "2026-09-21"]


def test_model_marks_deduplicate_changes_inside_one_period(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        for node, when, rater in (
            ("baseline", "2026-09-01T12:00:00Z", "rater-a"),
            ("b", "2026-09-15T12:00:00Z", "rater-b"),
            ("a-again", "2026-09-16T12:00:00Z", "rater-a"),
        ):
            _accepted(db, node, when)
            _rating(db, node, rater)
        db.commit()
        marks = [
            mark for mark in derive_breaks(db, ("estimated_human_hours_per_active_hour",), _grain())["estimated_human_hours_per_active_hour"]
            if mark["kind"] == "model"
        ]
    finally:
        db.close()
    assert [mark["period"] for mark in marks] == ["2026-09-14"]


def test_model_marks_follow_only_the_rating_that_counts(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db)
        for node, when in (("one", "2026-09-01T12:00:00Z"), ("two", "2026-09-15T12:00:00Z"), ("three", "2026-09-22T12:00:00Z")):
            _accepted(db, node, when)
            _rating(db, node, "rater-a", "2026-10-01T12:00:00Z")
            _rating(db, node, "rater-b", "2026-10-02T12:00:00Z")
        _accepted(db, "withdrawn", "2026-09-29T12:00:00Z")
        _rating(db, "withdrawn", "rater-c", "2026-10-01T12:00:00Z")
        _rating(db, "withdrawn", "rater-c", "2026-10-02T12:00:00Z", retracted=True)
        db.commit()
        marks = [
            mark for mark in derive_breaks(db, ("estimated_human_hours_per_active_hour",), _grain())["estimated_human_hours_per_active_hour"]
            if mark["kind"] == "model"
        ]
    finally:
        db.close()
    assert marks == []


def test_seeded_start_skips_its_live_query(isolated_db, monkeypatch) -> None:
    from api import measurement_breaks

    db = get_db()
    try:
        _thread(db)
        _mark(db, "2026-09-14T12:00:00Z", "start", "outcomes", "first recorded on this computer")
        db.commit()
        monkeypatch.setattr(measurement_breaks, "live_starts_query", lambda groups: (_ for _ in ()).throw(AssertionError(groups)))
        breaks = derive_breaks(db, ("outcomes",), _grain())
    finally:
        db.close()
    assert breaks["outcomes"][0]["period"] == "2026-09-14"


def test_start_only_callers_skip_the_mark_reads(isolated_db) -> None:
    db = get_db()
    statements: list[str] = []
    try:
        _thread(db)
        for node, when, rater in (("baseline", "2026-09-01T12:00:00Z", "rater-a"), ("b", "2026-09-15T12:00:00Z", "rater-b")):
            _accepted(db, node, when)
            _rating(db, node, rater)
        db.commit()
        db.set_trace_callback(statements.append)
        start = source_started_period(db, "estimated_human_hours_per_active_hour", _grain())
        db.set_trace_callback(None)
    finally:
        db.close()
    assert start == "2026-08-31"
    assert statements and not any("LAG(" in sql or "json_group_array" in sql for sql in statements)


def test_model_mark_uses_each_raters_earliest_accepted_settlement(isolated_db) -> None:
    grain = resolve_grain("days", "America/Los_Angeles", now=datetime(2026, 10, 22, tzinfo=timezone.utc))
    db = get_db()
    try:
        _thread(db)
        _accepted(db, "baseline", "2026-09-08T12:00:00Z")
        _accepted(db, "later", "2026-09-16T12:00:00Z")
        _accepted(db, "earlier", "2026-09-15T05:30:00Z")
        _rating(db, "baseline", "rater-a")
        _rating(db, "later", "rater-b")
        _rating(db, "earlier", "rater-b")
        db.commit()
        breaks = derive_breaks(db, ("estimated_human_hours_per_active_hour",), grain)
    finally:
        db.close()
    assert {"period": "2026-09-14", "kind": "model", "reason": "judging model changed"} in breaks[
        "estimated_human_hours_per_active_hour"
    ]


def test_availability_and_comparability_helpers_distinguish_start_and_marks() -> None:
    start_then_completeness = [
        {"period": "2026-03-02", "kind": "start", "reason": "first recorded on this computer"},
        {"period": "2026-09-07", "kind": "source", "reason": "token totals cover every agent from Sep 7, 2026 on"},
    ]
    marks = [
        {"period": "2026-08-31", "kind": "world", "reason": "counting changed"},
        {"period": "2026-09-14", "kind": "model", "reason": "judging model changed"},
    ]
    assert first_availability_period(start_then_completeness) == "2026-03-02"
    assert comparability_start(marks) == "2026-09-14"
    assert not period_comparable("2026-09-07", marks)
    assert period_comparable("2026-09-14", marks)


@pytest.mark.asyncio
async def test_delayed_agent_early_period_is_a_measured_zero(isolated_db) -> None:
    """A later scope cannot move an install-wide instrument start boundary."""
    db = get_db()
    try:
        _thread(db)
        _accepted(db, "early", "2026-08-10T12:00:00Z")
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
    before_start = _row("outcomes", "2026-08-03", None, True, grain=_grain(), source_start="2026-08-10")
    assert (before_start["value"], before_start["raw_value"], before_start["n"]) == (None, None, 0)
