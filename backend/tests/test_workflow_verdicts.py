"""Fixture tests for the slice-1 workflow verdict decision table."""
from __future__ import annotations

import sqlite3

import pytest

from core.workflow_verdict_evaluator import Evaluation, _write_one
from core.workflow_verdicts import (
    DecidingMeasure, WorkflowRun, decide, freeze_plan, measure_call, nearest_baseline, read_frozen_plan,
)
from infra.outcome_events_schema import OUTCOME_EVENTS_SCHEMA_SQL


def runs(version: str, value: float, *, count: int = 20, model: str = "model-a", difficulty: str = "standard",
         size: str = "M", counter: str | None = "counter-1") -> list[WorkflowRun]:
    return [WorkflowRun(f"{version}-{model}-{index}", version, model, difficulty, size, "2026-10-01T00:00:00+00:00",
                        {"quality": value, "cost": value, "corrections": value}, counter) for index in range(count)]


def plan(*measures: DecidingMeasure):
    return freeze_plan("workflow:skill@v2", "v1", measures)


@pytest.mark.parametrize(("old", "new", "expected"), [
    (1.0, 2.0, "worse"), (2.0, 1.0, "better"), (1.0, 1.0, "no_worse"),
])
def test_each_clear_measure_call(old: float, new: float, expected: str) -> None:
    measure = DecidingMeasure("quality", "lower", .1)
    assert measure_call(runs("v1", old) + runs("v2", new), "v2", plan(measure), measure).call == expected


def test_unclear_measure_call() -> None:
    measure = DecidingMeasure("quality", "lower", .1)
    noisy = runs("v2", .7, count=10) + runs("v2", 1.4, count=10)
    assert measure_call(runs("v1", 1.0) + noisy, "v2", plan(measure), measure).call == "unclear"


@pytest.mark.parametrize(("quality", "cost", "expected", "reason"), [
    ("worse", "better", "revert", "quality_worse"),
    ("better", "worse", "keep", "quality_better"),
    ("no_worse", "worse", "revert", "cost_worse_without_quality_gain"),
    ("no_worse", "better", "keep", "no_worse"),
])
def test_verdict_table(quality: str, cost: str, expected: str, reason: str) -> None:
    quality_values = {"worse": (1.0, 2.0), "better": (2.0, 1.0), "no_worse": (1.0, 1.0)}[quality]
    cost_values = {"worse": (1.0, 2.0), "better": (2.0, 1.0)}[cost]
    source = runs("v1", quality_values[0]) + runs("v2", quality_values[1])
    source = [WorkflowRun(run.run_id, run.version, run.model, run.difficulty, run.size_band, run.settled_at,
                          {"quality": run.values["quality"], "cost": cost_values[0] if run.version == "v1" else cost_values[1]},
                          run.correction_counter) for run in source]
    verdict = decide(source, "v2", plan(DecidingMeasure("quality", "lower", .1), DecidingMeasure("cost", "lower", .1, "spend")))
    assert (verdict.word, verdict.reason) == (expected, reason)


def test_unclear_waits_then_keeps_after_twice_the_minimum() -> None:
    measure = DecidingMeasure("quality", "lower", .1)
    first = runs("v1", 1.0) + runs("v2", .7, count=10) + runs("v2", 1.4, count=10)
    assert decide(first, "v2", plan(measure)).word == "collecting"
    doubled = first + runs("v1", 1.0, count=20) + runs("v2", .7, count=10) + runs("v2", 1.4, count=10)
    assert decide(doubled, "v2", plan(measure)).word == "keep"


def test_replaced_early_nearest_baseline_harm_anywhere_and_changed_counter() -> None:
    measure = DecidingMeasure("quality", "lower", .1)
    early = runs("v1", 1.0) + runs("v2", 1.0, count=4)
    assert decide(early, "v2", plan(measure), replaced_by="v3").word == "changed_early"
    versions = ["v0", "v1", "v2"]
    assert nearest_baseline(versions, runs("v0", 1.0) + runs("v1", 1.0, count=4), "v2") == "v0"
    harm = runs("v1", 1.0, model="a") + runs("v2", 2.0, model="a") + runs("v1", 1.0, model="b") + runs("v2", 0.0, model="b")
    assert decide(harm, "v2", plan(measure)).word == "revert"
    corrections = DecidingMeasure("corrections", "lower", .1)
    changed = runs("v1", 1.0, counter="old") + runs("v2", 1.0, counter="new")
    assert decide(changed, "v2", plan(corrections)).word == "not_enough_data"


def test_reached_verdict_does_not_flip_on_new_data(monkeypatch) -> None:
    import core.workflow_verdict_evaluator as evaluator

    monkeypatch.setattr(evaluator, "VERDICT_WRITES_ENABLED", True)
    db = sqlite3.connect(":memory:")
    db.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    frozen = plan(DecidingMeasure("quality", "lower", .1))
    first = Evaluation("agent", frozen, decide(runs("v1", 1.0) + runs("v2", 2.0), "v2", frozen), True)
    second = Evaluation("agent", frozen, decide(runs("v1", 2.0) + runs("v2", 1.0), "v2", frozen), False)
    assert _write_one(db, first) is True
    assert read_frozen_plan(db, frozen.subject) == frozen
    assert _write_one(db, second) is False
    row = db.execute("SELECT attributes FROM outcome_events WHERE event_name='measurement.verdict.reached'").fetchone()
    assert 'quality_worse' in row[0]
