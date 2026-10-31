"""Correction coverage: how many corrections were detected, triaged, or left on a thread that never ran again."""

from __future__ import annotations

import json

import pytest

from core.correction_attribution import person_correction_coverage
from core.correction_learning import TRIAGE_EVENT
from tests.test_measurement_api import WEEK, _seed_thread
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
async def test_the_already_said_series_is_registered_but_not_served(isolated_db, monkeypatch):
    from fastapi import HTTPException

    from api.measurement_registry import REGISTRY

    assert REGISTRY["already_said_check_passed_pct"].available is False
    with pytest.raises(HTTPException) as refused:
        await _rows(monkeypatch, "already_said_check_passed_pct")
    assert refused.value.status_code == 400


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
