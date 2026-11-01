"""Correction coverage: how many corrections were detected, triaged, or left on a thread that never ran again."""

from __future__ import annotations

import json

import pytest

from core.correction_attribution import person_correction_coverage
from core.correction_learning import TRIAGE_EVENT
from tests.test_measurement_api import USER, WEEK, _freeze_clock, _seed_thread
from tests.test_measurement_corrections_series import _activate, _fact, _rows, _seed

IDS = "corrections_detected,corrections_triaged_pct,corrections_untriaged_idle_pct,idle_untriaged_threads"
CURRENT = "2026-10-05"  # the week in progress under the frozen clock


def _triage(db, source_id: str, *, tier: str = "miss", at: str = "2026-09-25T00:00:00Z") -> None:
    key = f"message:{source_id}"
    db.execute(
        "INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,attributes) "
        "VALUES (?,?,?,'corsia','instrumented',?,?,?,?)",
        (f"triage:{key}", f"triage:{key}", TRIAGE_EVENT, at, at, key, json.dumps({"tier": tier})),
    )


def _turn(db, thread: str, started_at: str) -> None:
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES (?,?,0,'m','completed',?,'boot')",
        (f"turn-{thread}-{started_at}", thread, started_at),
    )


def _db():
    from infra.database import get_db

    return get_db()


def _scene(db) -> None:
    """Five corrections on four threads in one week."""
    _seed(db)
    for thread in ("busy", "archived"):
        _seed_thread(db, thread)
    db.execute("UPDATE threads SET archived_at='2026-09-24T00:00:00Z' WHERE id='archived'")
    _fact(db, "triaged", thread="root")
    _triage(db, "triaged", tier="note")
    _fact(db, "idle-root", thread="root")  # untriaged, root never runs again
    _fact(db, "idle-archived", thread="archived")  # untriaged, archived
    _fact(db, "idle-archived-2", thread="archived")  # a second one on the same thread
    _fact(db, "busy", thread="busy")  # untriaged, but the thread ran afterwards
    _turn(db, "busy", "2026-09-23T08:00:00Z")
    _turn(db, "root", "2026-09-21T08:00:00Z")  # before the corrections: not a later turn
    db.commit()


@pytest.mark.asyncio
async def test_each_formula_counts_triaged_untriaged_and_idle(isolated_db, monkeypatch):
    db = _db()
    try:
        _scene(db)
    finally:
        db.close()

    rows = await _rows(monkeypatch, IDS)
    assert rows[("corrections_detected", WEEK)]["value"] == 5
    triaged = rows[("corrections_triaged_pct", WEEK)]
    assert triaged["raw_value"] == pytest.approx(1 / 5) and triaged["n"] == 5
    idle = rows[("corrections_untriaged_idle_pct", WEEK)]
    assert idle["raw_value"] == pytest.approx(3 / 4) and idle["n"] == 4  # of the 4 untriaged, 3 sit on idle threads
    assert rows[("idle_untriaged_threads", WEEK)]["value"] == 2  # root and archived: the second archived one adds none


@pytest.mark.asyncio
async def test_a_triaged_correction_on_an_idle_thread_is_not_untriaged(isolated_db, monkeypatch):
    db = _db()
    try:
        _seed(db)
        _fact(db, "done")
        _triage(db, "done", tier="not_correction")  # any tier counts
        db.commit()
    finally:
        db.close()

    rows = await _rows(monkeypatch, IDS)
    assert rows[("corrections_triaged_pct", WEEK)]["raw_value"] == 1
    assert rows[("corrections_untriaged_idle_pct", WEEK)]["raw_value"] is None  # nothing untriaged
    assert rows[("idle_untriaged_threads", WEEK)]["value"] == 0


@pytest.mark.asyncio
async def test_ratios_withhold_below_minimum_n_and_serve_at_it(isolated_db, monkeypatch):
    db = _db()
    try:
        _seed(db)
        for number in range(20):
            _fact(db, f"m{number}")
        for number in range(5):
            _triage(db, f"m{number}")
        db.commit()
    finally:
        db.close()

    rows = await _rows(monkeypatch, IDS)
    assert rows[("corrections_triaged_pct", WEEK)]["value"] == pytest.approx(5 / 20)
    assert rows[("corrections_untriaged_idle_pct", WEEK)]["value"] is None  # 15 untriaged, below 20
    assert rows[("corrections_untriaged_idle_pct", WEEK)]["raw_value"] == 1
    assert rows[("corrections_detected", WEEK)]["value"] == 20


@pytest.mark.asyncio
async def test_the_period_in_progress_is_served_partial_and_a_late_triage_raises_an_old_period(isolated_db, monkeypatch):
    db = _db()
    try:
        _seed(db)
        _fact(db, "first")
        _fact(db, "old")
        _fact(db, "new", at="2026-10-06T12:00:00Z")
        _triage(db, "first", at="2026-09-22T13:00:00Z")  # the first triage fact starts the series
        _triage(db, "old", at="2026-10-06T13:00:00Z")  # triaged after its period ended
        db.commit()
    finally:
        db.close()

    rows = await _rows(monkeypatch, IDS)
    assert rows[("corrections_detected", CURRENT)]["partial"] is True
    assert rows[("corrections_detected", CURRENT)]["value"] == 1
    assert rows[("corrections_detected", WEEK)]["partial"] is False
    assert rows[("corrections_triaged_pct", WEEK)]["raw_value"] == 1


@pytest.mark.asyncio
async def test_coverage_is_not_measured_before_triage_is_recorded(isolated_db, monkeypatch):
    db = _db()
    try:
        _seed(db)
        _fact(db, "m1")
        db.commit()
    finally:
        db.close()

    rows = await _rows(monkeypatch, IDS)
    assert rows[("corrections_detected", WEEK)]["value"] == 1  # detection has its own start
    assert not [key for key in rows if key[0] in {
        "corrections_triaged_pct", "corrections_untriaged_idle_pct", "idle_untriaged_threads"}]


@pytest.mark.asyncio
async def test_a_triage_fact_does_not_start_the_coverage_series_in_a_period_before_it(isolated_db, monkeypatch):
    db = _db()
    try:
        _seed(db)
        _fact(db, "early", at="2026-09-08T12:00:00Z")
        _fact(db, "later")
        _triage(db, "later", at="2026-09-25T00:00:00Z")
        db.commit()
    finally:
        db.close()

    rows = await _rows(monkeypatch, IDS)
    assert ("corrections_detected", "2026-09-07") in rows
    assert ("corrections_triaged_pct", "2026-09-07") not in rows  # no period is drawn before the first triage fact


def test_a_deleted_thread_reads_as_idle_because_its_turn_rows_go_with_it(isolated_db):
    from infra.thread_driver import delete_thread_turns

    db = _db()
    try:
        _seed(db)
        _seed_thread(db, "gone")
        _fact(db, "m1", thread="gone", at="2026-09-22T12:00:00Z")
        _turn(db, "gone", "2026-09-23T08:00:00Z")
        window = ("2026-09-21 00", "2026-09-28 00")
        assert [(c.thread, c.triaged, c.later_turn) for c in person_correction_coverage(db, *window)] == [
            ("gone", False, True)]
        delete_thread_turns(db, "gone")
        assert [(c.thread, c.triaged, c.later_turn) for c in person_correction_coverage(db, *window)] == [
            ("gone", False, False)]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_windows_and_averages_count_a_thread_idle_in_several_periods_once(isolated_db, monkeypatch):
    from api.measurement import measurement_series

    db = _db()
    try:
        _seed(db)
        _seed_thread(db, "idle")
        _fact(db, "first", at="2026-09-01T12:00:00Z")
        _triage(db, "first", at="2026-09-01T13:00:00Z")  # coverage is measured from the week of Aug 31
        for week in ("2026-09-08", "2026-09-15", "2026-09-22", "2026-09-29"):
            _fact(db, f"idle-{week}", thread="idle", at=f"{week}T12:00:00Z")  # one idle correction each week
        db.commit()
    finally:
        db.close()

    _freeze_clock(monkeypatch)
    result = await measurement_series("corrections_detected,idle_untriaged_threads", project="all", agent="none",
                                      grain="weeks", timezone_name="UTC", user=USER)
    rows = {(row["series"], row["period"]): row for row in result["rows"]}
    assert [rows[("idle_untriaged_threads", week)]["value"] for week in ("2026-09-07", "2026-09-14", WEEK, "2026-09-28")] == [1, 1, 1, 1]
    # The four weeks hold one idle thread, not four; the average is the mean of the weeks' own values, so it sits on the bars.
    assert result["windows"]["idle_untriaged_threads"]["latest"]["value"] == 1
    assert rows[("idle_untriaged_threads", "2026-09-28")]["average"] == pytest.approx(1)
    # Detected corrections add up across the span, and the average is the per-week mean, not the span total.
    assert result["windows"]["corrections_detected"]["latest"]["value"] == 4
    assert rows[("corrections_detected", "2026-09-28")]["average"] == pytest.approx(1)


def test_a_correction_steered_into_a_running_turn_is_not_idle(isolated_db):
    db = _db()
    try:
        _seed(db)
        _fact(db, "steer", at="2026-09-22T12:00:00Z")
        _fact(db, "waiting", at="2026-09-22T12:30:00Z")
        _turn(db, "root", "2026-09-22T11:00:00Z")  # started before both, and none starts after
        db.execute("UPDATE messages SET active_turn_id=? WHERE id='steer'", ("turn-root-2026-09-22T11:00:00Z",))
        found = person_correction_coverage(db, "2026-09-21 00", "2026-09-28 00")
        assert sorted((c.arrived, c.later_turn) for c in found) == [
            ("2026-09-22T12:00:00Z", True), ("2026-09-22T12:30:00Z", False)]
    finally:
        db.close()


def test_only_the_active_classifier_version_is_read_and_arrival_is_compared_through_ts(isolated_db):
    db = _db()
    try:
        _seed(db)
        _fact(db, "active", at="2026-09-22T12:00:00Z")
        _fact(db, "filling", version="v2", at="2026-09-22T12:00:00Z")
        _turn(db, "root", "2026-09-22 13:00:00")  # a space-separated stamp after a T-separated arrival
        _turn(db, "root", "2026-09-22 11:00:00")
        _activate(db, "v1")
        found = person_correction_coverage(db, "2026-09-21 00", "2026-09-28 00")
        assert [(c.arrived, c.later_turn) for c in found] == [("2026-09-22T12:00:00Z", True)]
        db.execute("DELETE FROM active_turns WHERE started_at='2026-09-22 13:00:00'")
        assert [c.later_turn for c in person_correction_coverage(db, "2026-09-21 00", "2026-09-28 00")] == [False]
    finally:
        db.close()


def test_corrections_in_a_period_with_no_outcomes_stay_out_of_the_per_outcome_ratios(monkeypatch):
    from api import measurement_corrections
    from api.measurement_windows import _summed

    counts = {
        "2026-09-21": {"detected": 2, "triaged": 1, "untriaged": 1, "untriaged_idle": 0, "idle_threads": frozenset()},
        "2026-09-28": {"detected": 3, "triaged": 0, "untriaged": 3, "untriaged_idle": 1, "idle_threads": frozenset({"t"})},
    }
    monkeypatch.setattr(measurement_corrections, "corrections_by_period", lambda db, grain: counts)
    decisions = [{"period": "2026-09-21", "outcomes": 5, "raised": 1, "user_holds": 0}]
    pooled = _summed(measurement_corrections.corrections_family(None, None, decisions))
    # The per-outcome numerator pools only the outcome period: 2 corrections over 5 outcomes, not 5.
    assert (pooled["corrections"], pooled["outcomes"]) == (2, 5)
    # The coverage series still count every drawn period's corrections.
    assert pooled["detected"] == 5 and pooled["untriaged_idle"] == 1
