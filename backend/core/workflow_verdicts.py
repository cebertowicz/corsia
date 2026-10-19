"""Deterministic, side-by-side workflow verdicts (slice 1).

The evaluator supplies settled run facts from its read phase.  This module keeps
all statistics out of SQLite transactions and writes only immutable outcome
records when the deliberately-off switch is enabled.
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping, Sequence

POLICY_VERSION = "workflow-verdict-v1"
DEFAULT_MINIMUM = 20
GROUP_MINIMUM = 5
BOOTSTRAP_DRAWS = 2_000


@dataclass(frozen=True)
class WorkflowRun:
    """A settled run and its already-priced, deciding measure values.

    ``None`` means unknown.  Cost must come from ``priced_run_costs`` below;
    callers must never substitute ``llm_usage.cost_usd``.
    """

    run_id: str
    version: str
    model: str
    difficulty: str | None
    size_band: str | None
    settled_at: str
    values: Mapping[str, float | None]
    correction_counter: str | None = None

    @property
    def group(self) -> tuple[str, str, str]:
        return (self.model, self.difficulty or "unrated", self.size_band or "unsized")


@dataclass(frozen=True)
class DecidingMeasure:
    name: str
    better: str
    margin: float
    kind: str = "quality"
    minimum: int = DEFAULT_MINIMUM

    def __post_init__(self) -> None:
        if self.better not in {"higher", "lower"}:
            raise ValueError("better must be higher or lower")
        if self.kind not in {"quality", "spend"}:
            raise ValueError("kind must be quality or spend")


@dataclass(frozen=True)
class FrozenPlan:
    subject: str
    baseline: str
    measures: tuple[DecidingMeasure, ...]
    seed: int
    policy_version: str = POLICY_VERSION

    def encoded(self) -> str:
        return json.dumps({"subject": self.subject, "baseline": self.baseline,
                           "measures": [asdict(item) for item in self.measures],
                           "seed": self.seed, "policy_version": self.policy_version},
                          sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class MeasureCall:
    name: str
    call: str
    difference: float | None
    interval: tuple[float, float] | None
    groups: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class Verdict:
    word: str
    reason: str
    look: int
    calls: tuple[MeasureCall, ...]


def nearest_baseline(versions: Sequence[str], runs: Iterable[WorkflowRun], candidate: str,
                     minimum: int = DEFAULT_MINIMUM) -> str | None:
    """Return predecessor, walking back when it lacks enough settled runs."""
    before = list(versions[:versions.index(candidate)])
    counts: dict[str, int] = defaultdict(int)
    for run in runs:
        counts[run.version] += 1
    return next((version for version in reversed(before) if counts[version] >= minimum), None)


def freeze_plan(subject: str, baseline: str, measures: Iterable[DecidingMeasure]) -> FrozenPlan:
    """Freeze all policy inputs before any verdict is calculated."""
    ordered = tuple(measures)
    if not ordered or not any(item.kind == "quality" for item in ordered):
        raise ValueError("a frozen workflow plan needs a quality measure")
    seed = int.from_bytes(hashlib.sha256(subject.encode("utf-8")).digest()[:8], "big")
    return FrozenPlan(subject, baseline, ordered, seed=seed)


def read_frozen_plan(db: sqlite3.Connection, subject: str) -> FrozenPlan | None:
    """Read the immutable planned event during the evaluator's read phase."""
    row = db.execute("SELECT attributes FROM outcome_events WHERE dedupe_key=? AND retracted_at IS NULL",
                     (f"verdict:planned:{subject}",)).fetchone()
    if row is None:
        return None
    payload = json.loads(json.loads(row[0])["plan"])
    measures = tuple(DecidingMeasure(**item) for item in payload["measures"])
    return FrozenPlan(payload["subject"], payload["baseline"], measures, int(payload["seed"]), payload["policy_version"])


def _values(runs: Iterable[WorkflowRun], version: str, measure: str) -> dict[tuple[str, str, str], list[float]]:
    grouped: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for run in runs:
        value = run.values.get(measure)
        if run.version == version and value is not None:
            grouped[run.group].append(float(value))
    return grouped


def _interval(new: dict[tuple[str, str, str], list[float]], old: dict[tuple[str, str, str], list[float]],
              measure: DecidingMeasure, seed: int) -> tuple[float, tuple[float, float], tuple[tuple[str, str, str], ...]] | None:
    groups = tuple(sorted(group for group in new if len(new[group]) >= GROUP_MINIMUM and len(old.get(group, ())) >= GROUP_MINIMUM))
    if not groups:
        return None
    if sum(len(new[group]) for group in groups) < measure.minimum or sum(len(old[group]) for group in groups) < measure.minimum:
        return None
    weights = {group: len(new[group]) / sum(len(new[item]) for item in groups) for group in groups}
    direction = 1 if measure.better == "higher" else -1
    randomizer = random.Random(seed)
    differences: list[float] = []
    for _ in range(BOOTSTRAP_DRAWS):
        total = 0.0
        for group in groups:
            new_sample = [randomizer.choice(new[group]) for _ in new[group]]
            old_sample = [randomizer.choice(old[group]) for _ in old[group]]
            total += weights[group] * direction * (sum(new_sample) / len(new_sample) - sum(old_sample) / len(old_sample))
        differences.append(total)
    differences.sort()
    point = sum(differences) / len(differences)
    return point, (differences[int(.05 * len(differences))], differences[int(.95 * len(differences))]), groups


def _call_from_interval(interval: tuple[float, float], margin: float) -> str:
    if interval[1] <= -margin:
        return "worse"
    if interval[0] >= margin:
        return "better"
    if interval[0] > -margin:
        return "no_worse"
    return "unclear"


def measure_call(runs: Iterable[WorkflowRun], version: str, plan: FrozenPlan, measure: DecidingMeasure) -> MeasureCall:
    """Classify one frozen measure; all numeric differences point toward better.

    A qualifying group that alone reaches the full minimum can establish harm;
    pooling may not wash it out.
    """
    all_runs = tuple(runs)
    if measure.name == "corrections":
        counters = {run.correction_counter for run in all_runs if run.version in {version, plan.baseline}}
        if len(counters - {None}) > 1:
            return MeasureCall(measure.name, "not_comparable", None, None, ())
    new, old = _values(all_runs, version, measure.name), _values(all_runs, plan.baseline, measure.name)
    seed = plan.seed + sum(ord(char) for char in measure.name)
    outcome = _interval(new, old, measure, seed)
    if outcome is None:
        return MeasureCall(measure.name, "too_few", None, None, ())
    difference, interval, groups = outcome
    for group in groups:
        if len(new[group]) >= measure.minimum and len(old[group]) >= measure.minimum:
            group_outcome = _interval({group: new[group]}, {group: old[group]}, measure, seed + len(group))
            assert group_outcome is not None
            if _call_from_interval(group_outcome[1], measure.margin) == "worse":
                return MeasureCall(measure.name, "worse", difference, interval, groups)
    return MeasureCall(measure.name, _call_from_interval(interval, measure.margin), difference, interval, groups)


def decide(runs: Iterable[WorkflowRun], version: str, plan: FrozenPlan, *, replaced_by: str | None = None) -> Verdict:
    """Apply the fixed two-look rule.  A final result never reopens."""
    runs = tuple(runs)
    new_count = sum(run.version == version for run in runs)
    minimum = max(item.minimum for item in plan.measures)
    if replaced_by and new_count < minimum:
        return Verdict("changed_early", f"replaced_by:{replaced_by};runs:{new_count}", 0, ())
    calls = tuple(measure_call(runs, version, plan, measure) for measure in plan.measures)
    quality = tuple((measure, call) for measure, call in zip(plan.measures, calls) if measure.kind == "quality")
    spend = tuple(call for measure, call in zip(plan.measures, calls) if measure.kind == "spend")
    if any(call.call == "worse" for _, call in quality):
        return Verdict("revert", "quality_worse", 1, calls)
    if any(call.call == "better" for _, call in quality):
        return Verdict("keep", "quality_better", 1, calls)
    if quality and all(call.call == "no_worse" for _, call in quality):
        if any(call.call == "worse" for call in spend):
            return Verdict("revert", "cost_worse_without_quality_gain", 1, calls)
        return Verdict("keep", "no_worse", 1, calls)
    if any(call.call == "unclear" for _, call in quality):
        doubled = all(sum(run.version == side for run in runs) >= 2 * measure.minimum
                      for measure in plan.measures for side in (version, plan.baseline))
        return Verdict("keep" if doubled else "collecting", "no_clear_difference" if doubled else "wait_for_look_2", 2 if doubled else 1, calls)
    return Verdict("not_enough_data", "no_independent_quality_measure", 0, calls)


def priced_run_costs(db: sqlite3.Connection, params: dict[str, object]) -> dict[str, float | None]:
    """Return whole-chain API-equivalent spend by run, or unknown.

    This intentionally reuses Measurement's chain and price admission.  A run
    with any unpriceable chain row is unknown, never a false zero from the
    call-time ``llm_usage.cost_usd`` column.
    """
    from api.cost_identity import IdentityResolver
    from api.measurement_model_cost import materialize_chain, query_model_costs

    chain = materialize_chain(db, params)
    try:
        rows = query_model_costs(db, params, IdentityResolver(db), chain_rows=chain)
    finally:
        db.execute(f"DELETE FROM {chain}")
    costs: dict[str, float | None] = {}
    for row in rows:
        run_id = str(row["task_id"])
        amount = row.get("dollars")
        if row.get("outcome") != "priced" or amount is None:
            costs[run_id] = None
        elif run_id not in costs:
            costs[run_id] = float(amount)
        elif costs[run_id] is not None:
            known = costs[run_id]
            assert known is not None
            costs[run_id] = known + float(amount)
    return costs


def assert_evaluator_fields(specs: Iterable[Mapping[str, object]]) -> tuple[DecidingMeasure, ...]:
    """Select only definitions sync admitted as deciding, preserving their declared role."""
    result = []
    for spec in specs:
        if not spec.get("can_decide"):
            continue
        if spec.get("decides_as", "quality") not in {"quality", "spend"}:
            raise ValueError("decides_as must be quality or spend")
        if not spec.get("version_attr") and not spec.get("workflow_version", True):
            raise ValueError("can_decide KPI has no workflow version or version_attr")
        name, better, margin, minimum = spec.get("name"), spec.get("better"), spec.get("margin"), spec.get("min_n", DEFAULT_MINIMUM)
        if not isinstance(name, str) or not isinstance(better, str) or not isinstance(margin, (int, float)) or not isinstance(minimum, int):
            raise ValueError("deciding KPI has an invalid evaluator definition")
        result.append(DecidingMeasure(name, better, float(margin), str(spec.get("decides_as", "quality")), minimum))
    return tuple(result)
