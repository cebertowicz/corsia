"""Workflow keep-or-revert verdicts: each call row, each verdict rule and its reason inputs (design §3.2-§3.6).

The rule tests build runs directly; ``test_workflow_verdict_read.py`` builds them from real schema rows.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import sqrt
from statistics import NormalDist

from loguru import logger
import pytest

import core.workflow_verdict_evaluator as evaluator
import core.workflow_verdicts as verdicts
from core.workflow_verdict_looks import OBRIEN_FLEMING, look_confidence, minimum_detectable_change
from core.workflow_verdict_rules import (
    Evaluation, FrozenPlan, Snapshot, TwoArms, WorkflowHistory, baseline_runs, choose_baseline, compute, decide,
    decide_arms, freeze_plan,
)
from core.workflow_verdict_types import split_subject, subject_of
from core.workflow_verdicts import (
    BUILTIN_WINDOW_DAYS, CORRECTIONS, COST, MINUTES, MIXED, CorrectionFact, KpiRow, MeasureCall, PlannedMeasure, Run, measure_call,
    parse_ts,
)
from infra.outcome_events_schema import OUTCOME_EVENTS_SCHEMA_SQL
from tests.verdict_calibration_helpers import calibration_script

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)
GROUP = ("model-a", "standard", "M")
KPI = PlannedMeasure("escapes", "quality", "lower", "kpi", 0, 1, margin_gap=0.1)
_serial = iter(range(1_000_000))
# 1.25 corrections per outcome against a baseline of 1: a quarter worse, too noisy to call at 20 or 40 runs.
NOISY = [0, 0, 0, 5]


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def runs(version: str, corrections: int | list[int], *, count: int = 20, group=GROUP, counter: str = "c1",
         state: str = "accepted", cost: float | None = 1.0, ended: float = 10, started: float = 20,
         lag: float = 1, kpi: list[tuple[str, str, float]] | None = None, kpi_rows: bool = True) -> list[Run]:
    """``count`` settled runs; run i has ``corrections[i % len]`` corrections, ``lag`` days after it ended."""
    per_run = corrections if isinstance(corrections, list) else [corrections]
    made = []
    for index in range(count):
        number = next(_serial)
        facts = tuple(CorrectionFact("message", f"m{number}-{k}", _at(ended - lag), counter, True)
                      for k in range(per_run[index % len(per_run)]))
        # The test KPI settles in 0 days, so its rows land when the run ends.
        rows = {KPI.name: tuple(KpiRow(part, label, value, _at(ended)) for part, label, value in (kpi or []))}
        made.append(Run(f"{version}-{number}", version, group, _at(started - index * 0.01), _at(ended), state, facts,
                        cost, rows if kpi_rows else None))
    return made


def history(*sides: list[Run], definitions=()) -> WorkflowHistory:
    return WorkflowHistory("sdlc-process", "owner", tuple(run for side in sides for run in side), tuple(definitions))


def plan_for(source: WorkflowHistory, version: str = "v2", *extra: PlannedMeasure) -> FrozenPlan:
    plan = freeze_plan(source, version)
    return FrozenPlan(plan.subject, plan.version, plan.baseline, plan.skipped, (*plan.measures, *extra),
                      plan.minimum, plan.refusals, plan.seed)


def verdict(source: WorkflowHistory, *extra: PlannedMeasure, version: str = "v2", replaced_by: str | None = None):
    replaced = (replaced_by, source.first_use(replaced_by)) if replaced_by is not None else None
    return decide(source, plan_for(source, version, *extra), NOW, replaced)


def corrections_call(new: list[Run], old: list[Run], minimum: int = 20):
    measure = PlannedMeasure(CORRECTIONS, "quality", "lower", "builtin", BUILTIN_WINDOW_DAYS, floor=0.02)
    return measure_call(measure, new, old, minimum=minimum, now=NOW, seed=7)


# --- calls (§3.6 table) ---------------------------------------------------------------------------------------

@pytest.mark.parametrize(("old", "new", "expected"), [
    (1, 2, "worse"), (2, 1, "better"), (1, 1, "no_worse"), (1, NOISY, "unclear"),
])
def test_each_measure_call(old, new, expected) -> None:
    assert corrections_call(runs("v2", new), runs("v1", old)).call == expected


def test_margin_is_a_quarter_of_the_baseline_with_a_correction_floor() -> None:
    assert corrections_call(runs("v2", 1), runs("v1", 2)).margin == pytest.approx(0.5)
    assert corrections_call(runs("v2", 0), runs("v1", 0)).margin == pytest.approx(0.02)
    assert KPI.margin(123.0) == 0.1  # an owner KPI keeps its own target-to-review gap


def test_too_few_below_the_minimum() -> None:
    call = corrections_call(runs("v2", 1, count=12), runs("v1", 1))
    assert (call.call, call.n_new, call.n_base) == ("too_few", 12, 20)


def test_not_comparable_no_shared_groups() -> None:
    call = corrections_call(runs("v2", 1, group=("model-b", "hard", "M")), runs("v1", 1))
    assert (call.call, call.reason) == ("not_comparable", "no_shared_groups")


def test_not_comparable_counter_changed() -> None:
    call = corrections_call(runs("v2", 1, counter="c2"), runs("v1", 1, counter="c1"))
    assert (call.call, call.reason) == ("not_comparable", "counter_changed")


def test_equal_ids_in_distinct_correction_source_namespaces_do_not_share_a_counter() -> None:
    run = replace(
        runs("v2", 0, count=1)[0],
        corrections=(
            CorrectionFact("message", "same-id", _at(9), "c1", True),
            CorrectionFact("doc_comment", "same-id", _at(9), "c2", True),
        ),
    )

    call = corrections_call([run], [], minimum=0)
    assert (call.call, call.reason) == ("not_comparable", "counter_changed")


def _with_plan_facts(made: list[Run], per_run: int) -> list[Run]:
    """Add ``per_run`` sent-back facts, which no classifier judged, to each run."""
    return [replace(run, corrections=run.corrections + tuple(
        CorrectionFact("sent_back", f"{run.run_id}-r{k}", run.terminal_at, None, True) for k in range(per_run)))
        for run in made]


def test_plan_facts_count_beside_classified_facts_under_one_counter() -> None:
    call = corrections_call(_with_plan_facts(runs("v2", 1), 1), runs("v1", 1))
    assert (call.call, call.values) == ("worse", (1.0, 2.0))


def test_plan_facts_alone_are_counted() -> None:
    call = corrections_call(_with_plan_facts(runs("v2", 0), 2), _with_plan_facts(runs("v1", 0), 1))
    assert (call.call, call.values) == ("worse", (1.0, 2.0))


def test_not_comparable_coverage_under_half() -> None:
    measure = PlannedMeasure("escapes", "quality", "lower", "kpi", 0, 1, margin_gap=0.1, coverage=True)
    labelled = [("num", "observed", 1.0), ("den", "observed", 1.0)]
    new = runs("v2", 0, count=10, kpi=labelled) + runs("v2", 0, count=12, kpi=[("den", "self_reported", 1.0)])
    call = measure_call(measure, new, runs("v1", 0, kpi=labelled), minimum=20, now=NOW, seed=1)
    assert (call.call, call.reason) == ("not_comparable", "coverage_under_half")


def test_not_comparable_kpi_engine_missing() -> None:
    call = measure_call(KPI, runs("v2", 0, kpi_rows=False), runs("v1", 0, kpi_rows=False), minimum=20, now=NOW, seed=1)
    assert (call.call, call.reason) == ("not_comparable", "kpi_engine_missing")


def test_unconfirmed_numerator_rows_give_not_enough_data_with_k() -> None:
    rows = [("num", "self_reported", 1.0), ("den", "observed", 1.0)]
    # The paired-denominator flag comes from the KPI-row reader: the denominator row is allowed.
    new = [Run(r.run_id, r.version, r.group, r.started_at, r.terminal_at, r.state, (), 1.0,
               {KPI.name: (KpiRow("num", "self_reported", 1.0, r.terminal_at or "", True),
                           KpiRow("den", "observed", 1.0, r.terminal_at or ""))}) for r in runs("v2", 0, kpi=rows)]
    result = verdict(history(runs("v1", 0, started=40, ended=30,
                                  kpi=[("num", "observed", 0.0), ("den", "observed", 1.0)]), new), KPI)
    assert (result.word, result.reason, result.inputs) == ("not_enough_data", "kpi_unconfirmed", {"kpi": "escapes", "k": 20})


def test_kpi_n_counts_deciding_denominator_rows_with_allowed_labels() -> None:
    rows = [("num", "observed", 0.0), ("den", "observed", 1.0), ("den", "self_reported", 1.0)]
    call = measure_call(KPI, runs("v2", 0, kpi=rows), runs("v1", 0, kpi=rows), minimum=20, now=NOW, seed=1)
    assert (call.call, call.n_new) == ("no_worse", 20)


@pytest.mark.parametrize(("better", "new_num", "reason"), [
    ("lower", 1.05, "small_harm"), ("lower", 0.95, None),
    ("higher", 0.95, "small_harm"), ("higher", 1.05, None),
])
def test_small_harm_follows_the_measure_direction(better, new_num, reason) -> None:
    measure = PlannedMeasure("escapes", "quality", better, "kpi", 0, 1, margin_gap=0.1)
    old = runs("v1", 0, kpi=[("num", "observed", 1.0), ("den", "observed", 1.0)])
    new = runs("v2", 0, kpi=[("num", "observed", new_num), ("den", "observed", 1.0)])
    call = measure_call(measure, new, old, minimum=20, now=NOW, seed=1)
    assert (call.call, call.reason) == ("no_worse", reason)


def test_subject_round_trips_a_name_with_an_at_sign() -> None:
    assert split_subject(subject_of("workflow", "team@ops", "ab12cd")) == ("workflow", "team@ops", "ab12cd")


@pytest.mark.parametrize("new", [
    runs("v2", 1, count=19) + runs("v2", 1, count=1, cost=None),
    runs("v2", 1, count=19) + runs("v2", 1, count=1, cost=None, state="failed"),
    runs("v2", 1, count=20, state="failed"),  # no outcome to divide the spend by
])
def test_unknown_cost_makes_cost_per_outcome_unknown_never_zero(new) -> None:
    measure = PlannedMeasure(COST, "spend", "lower", "builtin", 0)
    call = measure_call(measure, new, runs("v1", 1), minimum=20, now=NOW, seed=1)
    assert (call.call, call.reason, call.values) == ("unknown", "cost_unknown", (None, None))


def test_failed_runs_count_in_the_total_but_never_as_outcomes() -> None:
    new = runs("v2", 1, count=15) + runs("v2", 1, count=5, state="failed")
    call = corrections_call(new, runs("v1", 1))
    # 20 runs reach the minimum; 20 corrections over 15 outcomes is 4/3 per outcome.
    assert (call.n_new, call.values[1]) == (20, pytest.approx(20 / 15))


def test_only_data_inside_the_window_counts_on_both_sides() -> None:
    inside = corrections_call(runs("v2", 1, lag=2.5), runs("v1", 1, lag=2.5))
    late_new = corrections_call(runs("v2", 1, lag=4), runs("v1", 1, lag=2.5))
    late_old = corrections_call(runs("v2", 1, lag=2.5), runs("v1", 1, lag=4))
    assert inside.values == (1.0, 1.0)
    assert late_new.values == (1.0, 0.0) and late_old.values == (0.0, 1.0)
    unsettled = corrections_call(runs("v2", 1, ended=2), runs("v1", 1))
    assert unsettled.call == "too_few"  # ended inside the 3-day window: not settled yet


def test_harm_in_one_group_is_worse_even_when_pooled_is_better() -> None:
    a, b = ("model-a", "standard", "M"), ("model-b", "standard", "M")
    new = runs("v2", 2, group=a) + runs("v2", 0, count=60, group=b)
    old = runs("v1", 1, group=a) + runs("v1", 1, count=40, group=b)
    call = corrections_call(new, old)
    assert (call.call, call.reason) == ("worse", "harm_in_group")


# --- baseline and frozen plan (§3.4) -------------------------------------------------------------------------

def test_baseline_is_the_nearest_version_with_the_minimum_settled_runs() -> None:
    source = history(runs("v0", 1, started=40, ended=30), runs("v1", 1, count=5, started=29, ended=25),
                     runs("v2", 1, started=20, ended=10))
    assert choose_baseline(source, "v2", 20, 3, NOW) == ("v0", ("v1",))
    assert plan_for(source).skipped == ("v1",) and verdict(source).inputs["skipped"] == ["v1"]


def test_a_later_run_in_a_new_group_never_changes_the_frozen_baseline() -> None:
    other = ("model-b", "hard", "L+")
    before = [runs("v0", 1, started=40, ended=30), runs("v1", 1, started=29, ended=25, group=other),
              runs("v2", 1, started=20, ended=10)]
    later = runs("v2", 1, count=1, started=5, ended=4, group=other)
    plans = {(freeze_plan(source, "v2").baseline, freeze_plan(source, "v2").skipped)
             for source in (history(*before), history(*before, later))}
    assert plans == {("v1", ())}
    assert verdict(history(*before)).reason == "no_shared_groups"


def test_a_rolled_back_version_keeps_its_full_history_place_in_the_order() -> None:
    source = history(
        runs("v1", 1, started=400, ended=390),
        runs("v2", 1, started=40, ended=30),
        runs("v1", 1, started=20, ended=10),
    )
    assert source.versions() == ["v1", "v2"]


def test_a_version_rolled_back_before_its_minimum_is_changed_again_by_the_version_it_returned_to() -> None:
    source = history(runs("v1", 1, started=40, ended=30), runs("v2", 1, count=4, started=20, ended=15),
                     runs("v1", 1, count=3, started=12, ended=10))
    assert source.successor("v2") == ("v1", _at(12))
    result = next(item for item in compute(Snapshot(NOW.isoformat(), (source,))) if item.plan.version == "v2")
    assert (result.verdict.word, result.verdict.reason) == ("changed_early", "replaced_before_look_1")
    assert {key: result.verdict.inputs[key] for key in ("k", "next")} == {"k": 4, "next": "v1"}
    current = history(runs("v1", 1, started=40, ended=30), runs("v2", 1, count=4, started=20, ended=15))
    assert current.successor("v2") is None and current.successor("v1") == ("v2", _at(20))


def test_a_successor_is_whichever_version_ran_next_after_its_last_run() -> None:
    back_to_middle = history(runs("v1", 1, started=40, ended=30), runs("v2", 1, count=4, started=20, ended=15),
                             runs("v3", 1, count=3, started=12, ended=10), runs("v2", 1, count=3, started=8, ended=6))
    assert back_to_middle.successor("v2") is None and back_to_middle.successor("v3") == ("v2", _at(8))
    returned_then_new = history(runs("v1", 1, started=40, ended=30), runs("v2", 1, count=4, started=20, ended=15),
                                runs("v1", 1, count=3, started=12, ended=10), runs("v3", 1, count=3, started=8, ended=6))
    assert returned_then_new.successor("v2") == ("v1", _at(12))


def test_after_a_rollback_the_baseline_is_the_version_actually_replaced() -> None:
    source = history(runs("v1", 1, started=90, ended=80), runs("v2", 2, started=70, ended=60),
                     runs("v1", 1, started=50, ended=45), runs("v3", 1, started=40, ended=30))
    assert source.predecessors("v3") == ["v1", "v2"] and source.successor("v1") == ("v3", _at(40))
    assert choose_baseline(source, "v3", 20, 3, parse_ts(source.first_use("v3"))) == ("v1", ())


def test_a_baseline_settling_after_first_use_never_changes_the_frozen_plan() -> None:
    # v1's runs end two days before v2's first use, so they settle only later; v0 stays the baseline.
    source = history(runs("v0", 0, started=90, ended=70), runs("v1", 1, started=60, ended=38),
                     runs("v2", 0, started=40, ended=30))
    plans = {at: next(e.plan for e in compute(Snapshot(at, (source,))) if e.plan.version == "v2")
             for at in (_at(37), NOW.isoformat())}
    assert {(plan.baseline, plan.skipped) for plan in plans.values()} == {("v0", ("v1",))}


def test_changed_again_is_never_stored_so_a_version_back_in_use_is_still_judged(monkeypatch) -> None:
    monkeypatch.setattr(evaluator, "VERDICT_WRITES_ENABLED", True)
    db = sqlite3.connect(":memory:")
    db.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    during_v1 = [runs("v1", 1, started=40, ended=30), runs("v2", 1, count=4, started=20, ended=15),
                 runs("v1", 1, count=3, started=12, ended=10)]
    early = next(e for e in compute(Snapshot(NOW.isoformat(), (history(*during_v1),))) if e.plan.version == "v2")
    assert early.verdict.word == "changed_early" and evaluator.write_verdicts(db, [early]) == 0
    # Clearly worse once back, so look 1 settles it (a no-gain version would wait for look 3).
    back = history(*during_v1, runs("v2", 2, count=20, started=9, ended=5))
    assert back.successor("v2") is None
    later = next(e for e in compute(Snapshot(NOW.isoformat(), (back,), {early.plan.subject: early.plan}))
                 if e.plan.version == "v2")
    assert later.verdict.word in {"keep", "revert"} and evaluator.write_verdicts(db, [later]) == 1


def test_frozen_plan_is_the_same_at_every_later_evaluation_time() -> None:
    source = history(
        runs("v1", 0, started=80, ended=50),
        runs("v1", 5, started=50, ended=38),
        runs("v2", 0, started=40, ended=40),
    )
    early = Snapshot(_at(37), (source,))
    later = Snapshot(NOW.isoformat(), (source,))
    early_evaluation = next(item for item in compute(early) if item.subject == "workflow:sdlc-process@v2")
    later_evaluation = next(item for item in compute(later) if item.subject == "workflow:sdlc-process@v2")
    assert early_evaluation == later_evaluation


def test_unsettled_predecessor_remains_a_named_no_baseline() -> None:
    source = history(runs("v1", 0, started=50, ended=38), runs("v2", 0, started=40, ended=40))
    plan = freeze_plan(source, "v2")
    assert plan.baseline is None
    assert decide(source, plan, NOW).reason == "no_baseline"


def test_calibration_splits_use_the_production_first_use_cutoff() -> None:
    source = history(
        runs("v1", 1, started=90, ended=80),
        runs("v1", 1, started=70, ended=60),
    )
    ordered = sorted(source.runs, key=lambda run: (run.started_at, run.run_id))
    midpoint = len(ordered) // 2
    halves = [replace(run, version="a" if index < midpoint else "b") for index, run in enumerate(ordered)]
    split = replace(source, runs=tuple(halves))
    calls, _ = calibration_script().splits(source, NOW)
    assert freeze_plan(split, "b").baseline == "a"
    assert any(call != "no_baseline" for found in calls.values() for call, _ in found)


def test_calibration_split_judges_the_same_new_side_as_production() -> None:
    # An unpriced mixed-model run is outside every comparison group, so it cannot make cost unknown.
    source = history(runs("v1", 1, started=90, ended=80), runs("v1", 1, started=70, ended=60),
                     runs("v1", 1, count=1, started=55, ended=50, group=(MIXED, "standard", "M"), cost=None))
    calls, _ = calibration_script().splits(source, NOW)
    assert [call for call, _ in calls[COST]] == ["no_worse"]


def test_calibration_split_refuses_without_a_production_baseline() -> None:
    calls, words = calibration_script().splits(history(runs("v1", 1)), NOW)
    assert words == [("not_enough_data", 20)]
    assert {call for found in calls.values() for call, _ in found} == {"no_baseline"}


def test_calibration_share_counts_only_splits_a_measure_could_measure() -> None:
    # A split with no baseline says nothing about false alarms, so it never dilutes the share toward quiet.
    script = calibration_script()
    assert script.aa_share([("no_baseline", None), ("no_baseline", None), ("worse", 0.4), ("no_worse", 0.1)]) == (2, 0.5)
    assert script.aa_share([("no_baseline", None)]) == (0, None)


def test_calibration_lists_a_deciding_kpi_no_split_held_as_not_calibrated() -> None:
    # Only a replay plan held the KPI, so no split called it: it must not read as calibrated.
    script = calibration_script()
    calls = {CORRECTIONS: [("no_worse", 0.1)], COST: [("no_baseline", None)]}
    expected = sorted(f"sdlc-process/{name}" for name in (COST, "escapes"))
    assert script.uncalibrated("sdlc-process", calls, {"escapes"}) == expected


def test_baseline_runs_are_the_most_recent_up_to_twice_the_minimum_per_group() -> None:
    older, recent = runs("v1", 1, count=10, started=70, ended=50), runs("v1", 1, count=45, started=60, ended=30)
    kept = baseline_runs(history(older, recent), "v1", 20, 3, NOW)
    assert len(kept) == 40 and {run.run_id for run in kept} <= {run.run_id for run in recent}


def test_collecting_before_the_minimum_and_not_enough_data_only_for_rule_5() -> None:
    collecting = verdict(history(runs("v1", 1, started=40, ended=30), runs("v2", 1, count=12)))
    assert (collecting.word, collecting.reason, collecting.inputs["n"], collecting.inputs["minimum"]) == (
        "collecting", "below_minimum", 12, 20)
    first = verdict(history(runs("v1", 1)), version="v1")
    assert (first.word, first.reason) == ("not_enough_data", "no_baseline")
    changed = verdict(history(runs("v1", 1, started=40, ended=30), runs("v2", 1, counter="c2")))
    assert (changed.word, changed.reason) == ("not_enough_data", "counter_changed")


# --- verdict rules (§3.6) ------------------------------------------------------------------------------------

def _priced(version: str, corrections, cost: float, **kw) -> list[Run]:
    return runs(version, corrections, cost=cost, **kw)


def test_rule_1_quality_worse_reverts() -> None:
    result = verdict(history(_priced("v1", 1, 1.0, started=40, ended=30), _priced("v2", 2, 0.5)))
    assert (result.word, result.reason, result.look, result.inputs["worse"]) == (
        "revert", "quality_worse", 1, [CORRECTIONS])


def test_rule_1_two_quality_measures_disagreeing_names_both() -> None:
    better = [("num", "observed", 0.0), ("den", "observed", 1.0)]
    worse = [("num", "observed", 1.0), ("den", "observed", 1.0)]
    old = runs("v1", 2, kpi=better, started=40, ended=30)
    result = verdict(history(old, runs("v2", 0, kpi=worse)), KPI)
    assert (result.word, result.reason) == ("revert", "quality_disagree")
    assert result.inputs["better"] == [CORRECTIONS] and result.inputs["worse"] == ["escapes"]
    flipped = verdict(history(runs("v1", 1, kpi=worse, started=40, ended=30), runs("v2", 1, kpi=better)), KPI)
    assert (flipped.word, flipped.reason, flipped.inputs["better"]) == ("keep", "quality_better", ["escapes"])


def test_rule_2_quality_better_keeps_and_names_a_worse_cost() -> None:
    result = verdict(history(_priced("v1", 2, 1.0, started=40, ended=30), _priced("v2", 1, 2.0)))
    assert (result.word, result.reason, result.inputs["cost_worse"]) == ("keep", "quality_better", [COST])


def test_rule_3_cost_worse_with_no_gain_reverts() -> None:
    result = verdict(history(_priced("v1", 1, 1.0, started=40, ended=30), _priced("v2", 1, 2.0)))
    assert (result.word, result.reason, result.inputs["cost_worse"]) == ("revert", "cost_worse_no_gain", [COST])


def test_rule_3_no_worse_and_cheaper_keeps_at_look_1() -> None:
    result = verdict(history(_priced("v1", 1, 1.0, started=40, ended=30), _priced("v2", 1, 0.5)))
    assert (result.word, result.reason, result.look) == ("keep", "no_worse", 1)
    assert result.inputs["cheaper"] is True


def test_rule_3_no_gain_not_cheaper_waits_for_look_3_before_reverting() -> None:
    old = _priced("v1", 1, 1.0, count=80, started=60, ended=30)
    first = verdict(history(old, _priced("v2", 1, 1.0, count=20)))
    assert (first.word, first.reason, first.look) == ("collecting", "wait_for_look_2", 1)
    second = verdict(history(old, _priced("v2", 1, 1.0, count=40)))
    assert (second.word, second.reason, second.look) == ("collecting", "wait_for_look_3", 2)
    final = verdict(history(old, _priced("v2", 1, 1.0, count=80)))
    assert (final.word, final.reason, final.look) == ("revert", "no_gain_not_cheaper", 3)
    assert "cheaper" not in final.inputs


def test_a_failed_run_in_each_arm_prices_in_and_a_clearly_cheaper_no_worse_version_is_kept() -> None:
    def arm(version: str, cost: float, **kw) -> list[Run]:
        # A failed run carries no corrections: with one each, corrections per outcome (20 over 18) varies with how
        # many failed runs a resample draws, and look 1's 99.31% reads it as unclear rather than no worse.
        return _priced(version, 1, cost, count=18, **kw) + _priced(version, 0, cost, count=2, state="failed", **kw)

    result = verdict(history(arm("v1", 1.0, started=40, ended=30), arm("v2", 0.5)))
    assert (result.word, result.reason) == ("keep", "no_worse")
    assert result.inputs["cheaper"] is True


def test_rule_3_unclear_spend_with_no_quality_gain_reverts() -> None:
    from core.workflow_verdict_rules import _rule

    calls = [
        MeasureCall(CORRECTIONS, "quality", "no_worse", difference=0.0, interval=(-0.01, 0.01), margin=0.02),
        MeasureCall(COST, "spend", "unclear"),
    ]

    assert _rule(calls, 1) is None and _rule(calls, 2) is None
    result = _rule(calls, 3)
    assert result is not None and (result.word, result.reason) == ("revert", "no_gain_not_cheaper")


def test_rule_4_unclear_at_40_continues_once_to_80_then_reverts() -> None:
    old = runs("v1", 1, count=80, started=60, ended=30)
    first = verdict(history(old, runs("v2", NOISY, count=20)))
    assert (first.word, first.reason, first.look) == ("collecting", "wait_for_look_2", 1)
    second = verdict(history(old, runs("v2", NOISY, count=40)))
    assert (second.word, second.reason, second.look) == ("collecting", "wait_for_look_3", 2)
    final = verdict(history(old, runs("v2", NOISY, count=80)))
    assert (final.word, final.reason, final.look) == ("revert", "unclear_after_look_3", 3)


def test_generic_two_arm_rule_does_not_depend_on_workflow_version_names() -> None:
    control = runs("anything", 1, count=20, started=40, ended=30)
    changed = runs("different", 2, count=20)
    source = history(control, changed)
    plan = plan_for(source, "different")

    result = decide_arms(plan, TwoArms(tuple(control), tuple(changed)), NOW)

    assert (result.word, result.reason, result.look) == ("revert", "quality_worse", 1)


def test_an_unclear_result_keeps_the_current_version_even_when_the_changed_arm_is_cheaper() -> None:
    from core.workflow_verdict_rules import _rule

    calls = [MeasureCall(CORRECTIONS, "quality", "unclear"), MeasureCall(COST, "spend", "better")]

    assert _rule(calls, 2) is None
    result = _rule(calls, 3)

    assert result is not None and (result.word, result.reason) == ("revert", "unclear_after_look_3")


def test_cheaper_arm_is_not_kept_when_its_interval_sits_wholly_on_the_harm_side() -> None:
    source = history(_priced("v1", 5, 1.0, started=40, ended=30), _priced("v2", 6, 0.5))

    result = verdict(source)

    assert (result.word, result.reason) == ("revert", "cheaper_small_harm")


def test_cheaper_arm_with_a_real_improvement_below_the_margin_is_kept() -> None:
    result = verdict(history(_priced("v1", 8, 1.0, started=40, ended=30), _priced("v2", 7, 0.5)))

    assert (result.word, result.reason) == ("keep", "no_worse")


def test_frozen_plan_carries_raised_confidence_and_reason() -> None:
    source = WorkflowHistory("sdlc-process", "owner", tuple(
        runs("v1", 1, started=40, ended=30) + runs("v2", 1)), verdict_confidence=0.95,
        verdict_confidence_reason="security")

    frozen = freeze_plan(source, "v2")

    assert FrozenPlan.decoded(frozen.encoded()).confidence == pytest.approx(0.95)
    assert FrozenPlan.decoded(frozen.encoded()).confidence_reason == "security"
    assert FrozenPlan.decoded(frozen.encoded()).policy_version == "workflow-verdict-v3"


@pytest.mark.parametrize(("confidence", "reason"), [
    (0.85, "security"), (0.9, None), (0.8, "security"),
])
def test_frozen_plan_rejects_unapproved_or_unexplained_confidence(confidence, reason) -> None:
    source = WorkflowHistory("sdlc-process", "owner", tuple(
        runs("v1", 1, started=40, ended=30) + runs("v2", 1)), verdict_confidence=confidence,
        verdict_confidence_reason=reason)

    with pytest.raises(ValueError, match="verdict confidence"):
        freeze_plan(source, "v2")


def test_a_look_reads_only_its_fixed_prefix_so_later_runs_do_not_move_it() -> None:
    old = runs("v1", 1, started=40, ended=30)
    at_look_1 = verdict(history(old, runs("v2", 2, ended=12)))
    later = verdict(history(old, runs("v2", 2, ended=12), runs("v2", 0, count=30, ended=5)))
    assert (at_look_1.word, at_look_1.reason) == (later.word, later.reason) == ("revert", "quality_worse")


def test_replaced_before_look_1_is_changed_again_with_its_run_count() -> None:
    source = history(runs("v1", 1, started=40, ended=30), runs("v2", 1, count=4, started=20, ended=15),
                     runs("v3", 1, count=3, started=12, ended=10))
    result = verdict(source, replaced_by="v3")
    assert (result.word, result.reason) == ("changed_early", "replaced_before_look_1")
    assert {key: result.inputs[key] for key in ("version", "k", "next")} == {"version": "v2", "k": 4, "next": "v3"}
    pending = history(runs("v1", 1, started=40, ended=30), runs("v2", 1, count=4, started=20, ended=1),
                      runs("v3", 1, count=3, started=12, ended=10))
    assert verdict(pending, replaced_by="v3").word == "collecting"


# --- frozen record and no flip (VR-3) ------------------------------------------------------------------------

def test_reached_verdict_does_not_flip_on_new_data(monkeypatch) -> None:
    monkeypatch.setattr(evaluator, "VERDICT_WRITES_ENABLED", True)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    source = history(runs("v1", 1, started=40, ended=30), runs("v2", 2))
    frozen = plan_for(source)
    first = Evaluation(frozen.subject, "owner", frozen, decide(source, frozen, NOW), True)
    flipped = history(runs("v1", 2, started=40, ended=30), runs("v2", 1))
    second = Evaluation(frozen.subject, "owner", frozen, decide(flipped, frozen, NOW), False)
    assert second.verdict.word == "keep"
    assert evaluator.write_verdicts(db, [first]) == 1
    assert evaluator.write_verdicts(db, [second]) == 0
    (row,) = db.execute("SELECT attributes FROM outcome_events WHERE event_name='measurement.verdict.reached'").fetchall()
    assert '"quality_worse"' in row["attributes"]
    (planned,) = db.execute("SELECT attributes FROM outcome_events WHERE event_name='measurement.verdict.planned'").fetchall()
    assert FrozenPlan.decoded(json.loads(planned["attributes"])["plan"]) == frozen
    snapshot = Snapshot(NOW.isoformat(), (flipped,), {}, frozenset({frozen.subject}))
    assert [e.subject for e in compute(snapshot)] == ["workflow:sdlc-process@v1"]


def _wake_db(*owners: tuple[str, str, str | None]) -> sqlite3.Connection:
    """Outcome events plus the thread and message columns a wake reads; each owner is (id, state, archived_reason)."""
    db = sqlite3.connect(":memory:")
    db.executescript(OUTCOME_EVENTS_SCHEMA_SQL + "CREATE TABLE threads (id TEXT PRIMARY KEY, lifecycle_state TEXT, "
                     "archived_reason TEXT); CREATE TABLE messages (id TEXT PRIMARY KEY);")
    db.executemany("INSERT INTO threads VALUES (?,?,?)", owners)
    return db


def _accepting(wakes: list):
    """A wake that is accepted the way the real one is: its message row exists afterwards."""
    def wake(thread_id, event, summary, details, *, message_id, db):
        wakes.append((thread_id, event, summary, details, message_id))
        db.execute("INSERT INTO messages (id) VALUES (?)", (message_id,))
    return wake


def test_switched_on_reached_verdict_wakes_its_owner_once(monkeypatch) -> None:
    from events import delivery

    monkeypatch.setattr(evaluator, "VERDICT_WRITES_ENABLED", True)
    wakes: list = []
    monkeypatch.setattr(delivery, "enqueue_event_wake", _accepting(wakes))
    db = _wake_db(("owner", "active", None))
    source = history(runs("v1", 1, started=40, ended=30), runs("v2", 2))
    frozen = plan_for(source)
    reached = Evaluation(frozen.subject, "owner", frozen, decide(source, frozen, NOW), True)

    assert evaluator.write_verdicts(db, [reached]) == 1
    assert evaluator.wake_owners(db) == 1
    assert evaluator.write_verdicts(db, [reached]) == 0
    assert evaluator.wake_owners(db) == 0
    logged: list[str] = []
    sink = logger.add(logged.append, level="WARNING", format="{message}")
    orphan = history(runs("v1", 1, started=40, ended=30), runs("v3", 2))
    unowned = plan_for(orphan, "v3")
    assert evaluator.write_verdicts(db, [Evaluation(unowned.subject, None, unowned, decide(orphan, unowned, NOW), True)]) == 1
    assert evaluator.wake_owners(db) == 0
    logger.remove(sink)
    assert [line.strip() for line in logged] == [f"verdict_wake: no owner to wake for {unowned.subject}"]
    assert wakes == [("owner", "measurement.verdict.reached",
                      f"Workflow sdlc-process ({frozen.version}) verdict reached: revert the change.",
                      {"subject": frozen.subject, "verdict": "revert", "reason": "quality_worse"},
                      f"verdict:wake:{frozen.subject}")]


def test_a_refused_wake_keeps_every_verdict_and_reaches_the_owner_once_fixed(monkeypatch) -> None:
    from events import delivery
    from infra.send_types import SendRejected

    wakes: list = []
    accept = _accepting(wakes)
    capped = {"capped"}

    def refuse(thread_id, *args, **kwargs):
        if thread_id in capped:
            raise SendRejected(409, "model's cap can't fit a checkpoint")
        accept(thread_id, *args, **kwargs)

    monkeypatch.setattr(evaluator, "VERDICT_WRITES_ENABLED", True)
    monkeypatch.setattr(delivery, "enqueue_event_wake", refuse)
    db = _wake_db(("capped", "active", None), ("owner", "active", None), ("purged", "purged", None),
                  ("archived", "archived", "user"))
    batch = []
    for owner, version in (("capped", "v2"), ("owner", "v3"), ("purged", "v4"), ("archived", "v5")):
        source = history(runs("v1", 1, started=40, ended=30), runs(version, 2))
        plan = plan_for(source, version)
        batch.append(Evaluation(plan.subject, owner, plan, decide(source, plan, NOW), True))
    logged: list[str] = []
    sink = logger.add(logged.append, level="WARNING", format="{message}")
    assert evaluator.write_verdicts(db, batch) == 4
    assert evaluator.wake_owners(db) == 1
    capped.clear()  # the person raises the cap; the next night's run reaches that owner
    assert evaluator.wake_owners(db) == 1
    assert evaluator.wake_owners(db) == 0
    logger.remove(sink)
    assert [line.strip() for line in logged] == [
        f"verdict_wake: owner capped refused for {batch[0].subject}: model's cap can't fit a checkpoint"]
    assert [wake[0] for wake in wakes] == ["owner", "capped"]


def test_each_look_is_judged_at_its_obrien_fleming_confidence() -> None:
    assert [round(look_confidence(0.8, look), 4) for look in (1, 2, 3)] == [0.9931, 0.9438, 0.8231]
    assert [round(look_confidence(0.9, look), 4) for look in (1, 2, 3)] == [0.9992, 0.9824, 0.9068]
    assert [round(look_confidence(0.95, look), 4) for look in (1, 2, 3)] == [0.9999, 0.9948, 0.9520]
    assert set(OBRIEN_FLEMING) == {verdicts.DEFAULT_CONFIDENCE, *verdicts.RAISED_CONFIDENCES}
    with pytest.raises(ValueError, match="O'Brien-Fleming"):
        look_confidence(0.85, 1)


@pytest.mark.parametrize("confidence", sorted(OBRIEN_FLEMING))
def test_obrien_fleming_constants_hold_the_trials_level_over_the_three_looks(confidence) -> None:
    """Re-derive the method recorded beside the constants: the chance that any look excludes zero on a version
    that changed nothing is one minus the confidence (|B(t)| < c at t = 1/4, 1/2, 1, Brownian motion)."""
    import numpy as np
    from scipy.stats import multivariate_normal

    times = np.array([0.25, 0.5, 1.0])
    edge = np.full(3, OBRIEN_FLEMING[confidence])
    quiet = multivariate_normal(np.zeros(3), np.minimum.outer(times, times), abseps=1e-9, releps=1e-9,
                                maxpts=3_000_000).cdf(edge, lower_limit=-edge)
    assert 1 - quiet == pytest.approx(1 - confidence, abs=2e-3)


def test_the_bootstrap_tail_expands_with_the_smaller_sides_run_count() -> None:
    assert [round(verdicts._tail(0.2, count), 4) for count in (20, 40, 80)] == [0.0866, 0.0934, 0.0967]
    assert verdicts._tail(0.2, 1) == verdicts._tail(0.2, 2)


def test_interval_takes_its_tails_from_the_smaller_side(monkeypatch) -> None:
    seen: list[tuple[float, int]] = []

    def tail(alpha: float, n: int) -> float:
        seen.append((alpha, n))
        return alpha / 2

    monkeypatch.setattr(verdicts, "_tail", tail)
    sample = verdicts._Sample(1.0, 1.0, 1)
    verdicts._interval({GROUP: [sample] * 30}, {GROUP: [sample] * 20}, [GROUP], 1, 0.8)
    assert seen == [(pytest.approx(0.2), 20)]


def test_minimum_detectable_change_is_hand_computed_per_look_and_none_under_two_baseline_runs() -> None:
    old = runs("v1", [0, 2], count=20, started=60, ended=30)  # per-run values 0 and 2: spread sqrt(20/19)
    spread = sqrt(20 / 19)
    z_power = NormalDist().inv_cdf(0.8)
    frozen = freeze_plan(history(old, runs("v2", 1, count=20)), "v2")
    # z(1 - a/2) at a look is c / sqrt(information fraction): 2.7008, 1.9098, 1.3504 for c = 1.3504. The baseline
    # arm is frozen at its 20 runs, so every look compares 20, 40 and 80 new runs against those 20.
    expected = [(2.7008 + z_power) * spread * sqrt(1 / 20 + 1 / 20), (1.9098 + z_power) * spread * sqrt(1 / 40 + 1 / 20),
                (1.3504 + z_power) * spread * sqrt(1 / 80 + 1 / 20)]
    assert frozen.mdc[CORRECTIONS] == pytest.approx(expected, rel=1e-3)
    assert MINUTES not in frozen.mdc  # shown, never deciding
    assert FrozenPlan.decoded(frozen.encoded()).mdc == frozen.mdc
    assert minimum_detectable_change(0.8, 1, None, 20) is None
    sparse = runs("v1", 1, count=1, started=60, ended=30) + runs("v1", 1, count=19, state="failed", started=60, ended=30)
    one_run = freeze_plan(history(sparse, runs("v2", 1, count=20)), "v2")
    assert one_run.baseline == "v1" and one_run.mdc[CORRECTIONS] == (None, None, None)


def test_no_baseline_freezes_none_never_zero_and_no_rule_reads_the_figure() -> None:
    new_only = freeze_plan(history(runs("v2", 1, count=20)), "v2")
    assert new_only.baseline is None and set(new_only.mdc[CORRECTIONS]) == {None}
    source = history(runs("v1", 1, count=80, started=60, ended=30), runs("v2", [0, 0, 3, 3, 4], count=40))
    frozen = plan_for(source)
    bare = replace(frozen, mdc={name: (0.0, 0.0, 0.0) for name in frozen.mdc})
    assert decide(source, replace(frozen, mdc={}), NOW) == decide(source, bare, NOW)


def test_a_clear_difference_at_80_percent_waits_for_the_stricter_first_look() -> None:
    """Judged at 80% at every look this version is reverted at look 1 (n=20); at look 1's 99.31% it is not clear,
    so it waits, and look 2's 94.38% still finds the same harm."""
    old = runs("v1", 1, count=80, started=60, ended=30)
    first = verdict(history(old, runs("v2", [0, 0, 3, 3, 4], count=20)))
    assert (first.word, first.reason, first.look) == ("collecting", "wait_for_look_2", 1)
    second = verdict(history(old, runs("v2", [0, 0, 3, 3, 4], count=40)))
    assert (second.word, second.reason, second.look) == ("revert", "quality_worse", 2)


def test_calibration_script_simulates_versions_that_changed_nothing_without_a_database(capsys) -> None:
    script = calibration_script()
    assert script.main(["--null-trials", "2", "--seed", "7"]) == 0
    assert capsys.readouterr().out.startswith("null trials 2: false calls ")
    assert script.main(["--null-trials", "2", "--fixed-baseline"]) == 0
    assert script.null_trial((7, 80, False)) == script.null_trial((7, 80, False))  # seeded, so splittable


def test_writes_stay_off_by_default() -> None:
    db = sqlite3.connect(":memory:")
    db.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    source = history(runs("v1", 1, started=40, ended=30), runs("v2", 2))
    evaluation = compute(Snapshot(NOW.isoformat(), (source,)))[-1]
    assert evaluator.VERDICT_WRITES_ENABLED is False and evaluator.write_verdicts(db, [evaluation]) == 0
    assert db.execute("SELECT COUNT(*) FROM outcome_events").fetchone()[0] == 0
