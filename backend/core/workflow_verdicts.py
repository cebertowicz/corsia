"""Workflow keep-or-revert verdicts: runs, measures and the call for each measure (verdict design §3.2-§3.6).

Pure code, no database. ``core.workflow_verdict_read`` builds the runs from real history and
``core.workflow_verdict_rules`` chooses the baseline, freezes the plan and applies the verdict rule.
"""
from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Sequence

POLICY_VERSION = "workflow-verdict-v1"
DEFAULT_MINIMUM = 20
GROUP_MINIMUM = 5
BOOTSTRAP_DRAWS = 2_000
DEFAULT_CONFIDENCE = 0.8
RAISED_CONFIDENCES = frozenset({0.9, 0.95})
BUILTIN_WINDOW_DAYS = 3
BUILTIN_MARGIN = 0.25
CORRECTION_FLOOR = 0.02
# KPI design §4.6: Imported and stronger let a deciding KPI decide; the rest never do.
ALLOWED_LABELS = frozenset({"observed", "imported", "labeled", "corroborated"})
OUTCOME_STATES = frozenset({"accepted", "accepted_with_followup"})
TERMINAL_STATES = OUTCOME_STATES | {"failed", "closed"}
MIXED = "mixed"
CORRECTIONS, COST, MINUTES = "corrections_per_outcome", "cost_per_outcome", "minutes_per_outcome"
# Calls that keep a measure from deciding at all, as opposed to waiting for runs.
BLOCKED = frozenset({"not_comparable", "unconfirmed"})

Group = tuple[str, str, str]


def parse_ts(value: str) -> datetime:
    """A stored timestamp in either separator form; a zoneless one is UTC."""
    parsed = datetime.fromisoformat(value.replace(" ", "T").replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class CorrectionFact:
    source_kind: str
    source_id: str
    source_at: str
    counter: str
    is_correction: bool


@dataclass(frozen=True)
class KpiRow:
    """One row behind a deciding KPI, from the KPI-row reader (KPI slice 2's formula engine)."""

    part: str  # "num" or "den"
    label: str
    value: float
    ts: str
    paired_den_allowed: bool = False  # a numerator row whose denominator row is allowed (rule 3)


@dataclass(frozen=True)
class Run:
    """One workflow run: the root plan node carrying the workflow and its settled facts."""

    run_id: str
    version: str
    group: Group
    started_at: str
    terminal_at: str | None
    state: str
    corrections: tuple[CorrectionFact, ...] = ()
    cost: float | None = None
    kpi_rows: Mapping[str, tuple[KpiRow, ...]] | None = None  # None: no KPI-row reader yet

    @property
    def outcome(self) -> bool:
        return self.state in OUTCOME_STATES

    def settled(self, window_days: int, now: datetime) -> bool:
        """Terminal, and its follow-up window has passed."""
        return (self.terminal_at is not None and self.state in TERMINAL_STATES
                and parse_ts(self.terminal_at) + timedelta(days=window_days) <= now)

    def in_window(self, ts: str, window_days: int) -> bool:
        assert self.terminal_at is not None
        return parse_ts(ts) <= parse_ts(self.terminal_at) + timedelta(days=window_days)


@dataclass(frozen=True)
class PlannedMeasure:
    """One measure in a frozen plan. Built-ins carry a relative margin; owner KPIs their own gap."""

    name: str
    role: str  # quality, spend or shown
    better: str
    source: str  # builtin or kpi
    window_days: int
    definition_version: int | None = None
    margin_gap: float | None = None
    floor: float = 0.0
    coverage: bool = False
    guard_against_harm: bool = False

    def margin(self, baseline_value: float) -> float:
        if self.margin_gap is not None:
            return self.margin_gap
        return max(BUILTIN_MARGIN * abs(baseline_value), self.floor)


@dataclass(frozen=True)
class MeasureCall:
    name: str
    role: str
    call: str
    reason: str | None = None
    difference: float | None = None
    interval: tuple[float, float] | None = None
    margin: float | None = None
    groups: tuple[Group, ...] = ()
    n_new: int = 0
    n_base: int = 0
    values: tuple[float | None, float | None] = (None, None)  # baseline, new
    detail: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _Sample:
    num: float
    den: float
    n: int


def _counter(measure: PlannedMeasure, runs: Sequence[Run]) -> str | None | bool:
    """The one classifier version that counted every source on both sides, None for none, False if none did."""
    by_source: dict[tuple[str, str], set[str]] = defaultdict(set)
    for run in runs:
        for fact in run.corrections:
            if run.in_window(fact.source_at, measure.window_days):
                by_source[(fact.source_kind, fact.source_id)].add(fact.counter)
    if not by_source:
        return None
    common = set.intersection(*by_source.values())
    return max(common) if common else False


def _sample(measure: PlannedMeasure, run: Run, counter: str | None) -> _Sample | None:
    den = 1.0 if run.outcome else 0.0
    if measure.name == CORRECTIONS:
        count = sum(1 for fact in run.corrections if fact.is_correction and fact.counter == counter
                    and run.in_window(fact.source_at, measure.window_days))
        return _Sample(float(count), den, 1)
    if measure.name == COST:  # an unknown cost was already called unknown, never 0
        return None if run.cost is None else _Sample(run.cost, den, 1)
    rows = [row for row in (run.kpi_rows or {}).get(measure.name, ())
            if row.label in ALLOWED_LABELS and run.in_window(row.ts, measure.window_days)]
    dens = sum(1 for row in rows if row.part == "den")
    return _Sample(sum(row.value for row in rows if row.part == "num"), float(dens), dens)


def _blocked(measure: PlannedMeasure, new: Sequence[Run], old: Sequence[Run]) -> MeasureCall | None:
    """The calls decided before any number: not comparable, unconfirmed or unknown."""
    runs = [*new, *old]

    def call(name: str, reason: str, **detail: object) -> MeasureCall:
        return MeasureCall(measure.name, measure.role, name, reason, detail=detail)

    if measure.role == "shown":
        return call("shown", "not_attributed_per_run")
    if measure.source == "kpi":
        if any(run.kpi_rows is None for run in runs):
            return call("not_comparable", "kpi_engine_missing")
        rows = [row for run in runs for row in (run.kpi_rows or {}).get(measure.name, ())
                if run.in_window(row.ts, measure.window_days)]
        unconfirmed = sum(1 for row in rows if row.part == "num" and row.label not in ALLOWED_LABELS
                          and row.paired_den_allowed)
        if unconfirmed:
            return call("unconfirmed", "kpi_unconfirmed", kpi=measure.name, k=unconfirmed)
        if measure.coverage:
            for side in (new, old):
                labelled = sum(1 for run in side if any(row.label in ALLOWED_LABELS
                                                        for row in (run.kpi_rows or {}).get(measure.name, ())))
                if side and labelled * 2 < len(side):
                    return call("not_comparable", "coverage_under_half", kpi=measure.name)
    if measure.name == COST:
        if any(not run.outcome for run in runs):
            return call("unknown", "failed_run_unpriced")
        if any(run.cost is None for run in runs):
            return call("unknown", "cost_unknown")
    return None


def _grouped(measure: PlannedMeasure, runs: Iterable[Run], counter: str | None) -> dict[Group, list[_Sample]]:
    grouped: dict[Group, list[_Sample]] = defaultdict(list)
    for run in runs:
        sample = _sample(measure, run, counter)
        if sample is not None:
            grouped[run.group].append(sample)
    return grouped


def _value(samples: Sequence[_Sample]) -> float | None:
    den = sum(sample.den for sample in samples)
    return None if den == 0 else sum(sample.num for sample in samples) / den


def _pooled(new: Mapping[Group, Sequence[_Sample]], old: Mapping[Group, Sequence[_Sample]],
            groups: Sequence[Group]) -> tuple[float, float, float] | None:
    """Within-group differences averaged with the new version's weights: (difference, baseline, new)."""
    total = diff = base = fresh = 0.0
    for group in groups:
        new_value, old_value = _value(new[group]), _value(old[group])
        if new_value is None or old_value is None:
            continue
        weight = float(sum(sample.n for sample in new[group]))
        total, diff = total + weight, diff + weight * (new_value - old_value)
        base, fresh = base + weight * old_value, fresh + weight * new_value
    return None if total == 0 else (diff / total, base / total, fresh / total)


def _interval(new: Mapping[Group, Sequence[_Sample]], old: Mapping[Group, Sequence[_Sample]],
              groups: Sequence[Group], seed: int, confidence: float) -> tuple[float, float]:
    """A configurable bootstrap interval, resampled within each side and group."""
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    rng, draws = random.Random(seed), []
    for _ in range(BOOTSTRAP_DRAWS):
        resampled_new = {g: [rng.choice(new[g]) for _ in new[g]] for g in groups}
        resampled_old = {g: [rng.choice(old[g]) for _ in old[g]] for g in groups}
        pooled = _pooled(resampled_new, resampled_old, groups)
        if pooled is not None:
            draws.append(pooled[0])
    draws.sort()
    tail = (1 - confidence) / 2
    return draws[int(tail * (len(draws) - 1))], draws[int((1 - tail) * (len(draws) - 1))]


def _call_from(better: str, difference: float, interval: tuple[float, float], margin: float) -> str:
    harm = difference if better == "lower" else -difference
    low, high = interval if better == "lower" else (-interval[1], -interval[0])
    if low > 0 and harm >= margin:
        return "worse"
    if high < 0 and -harm >= margin:
        return "better"
    return "no_worse" if high < margin else "unclear"


def eligible(measure: PlannedMeasure, new: Sequence[Run], old: Sequence[Run], counter: str | None
             ) -> tuple[dict[Group, list[_Sample]], dict[Group, list[_Sample]], list[Group], list[Group]]:
    """Both sides' samples, the groups they share and the shared groups with enough runs on each side."""
    new_g, old_g = _grouped(measure, new, counter), _grouped(measure, old, counter)
    shared = sorted(set(new_g) & set(old_g))
    ok = [g for g in shared if sum(s.n for s in new_g[g]) >= GROUP_MINIMUM and sum(s.n for s in old_g[g]) >= GROUP_MINIMUM
          and _value(new_g[g]) is not None and _value(old_g[g]) is not None]
    return new_g, old_g, shared, ok


def measure_call(measure: PlannedMeasure, new: Sequence[Run], old: Sequence[Run], *, minimum: int,
                 now: datetime, seed: int, confidence: float = DEFAULT_CONFIDENCE,
                 bootstrap: bool = True, base_minimum: int | None = None) -> MeasureCall:
    """One measure's call at a look: Worse, Better, No worse, Unclear, Too few, Not comparable or unknown.

    ``base_minimum`` (default ``minimum``) is the baseline side's bar: look 2 doubles only the new side's,
    since the baseline keeps at most twice the minimum per group.
    """
    new = [run for run in new if run.settled(measure.window_days, now)]
    old = [run for run in old if run.settled(measure.window_days, now)]
    blocked = _blocked(measure, new, old)
    if blocked is not None:
        return blocked
    counter = _counter(measure, [*new, *old]) if measure.name == CORRECTIONS else None
    if counter is False:
        return MeasureCall(measure.name, measure.role, "not_comparable", "counter_changed")
    new_g, old_g, shared, groups = eligible(measure, new, old, counter or None)  # type: ignore[arg-type]
    if not new_g:
        return MeasureCall(measure.name, measure.role, "too_few", "below_minimum")
    if not shared:
        return MeasureCall(measure.name, measure.role, "not_comparable", "no_shared_groups")
    n_new = sum(s.n for g in groups for s in new_g[g])
    n_old = sum(s.n for g in groups for s in old_g[g])
    base = MeasureCall(measure.name, measure.role, "too_few", "below_minimum", groups=tuple(groups), n_new=n_new, n_base=n_old)
    if n_new < minimum or n_old < (minimum if base_minimum is None else base_minimum):
        return base
    pooled = _pooled(new_g, old_g, groups)
    assert pooled is not None
    difference, baseline_value, new_value = pooled
    margin = measure.margin(baseline_value)
    if not bootstrap:
        return replace(base, call="counted", reason=None, margin=margin)
    interval = _interval(new_g, old_g, groups, seed, confidence)
    call, reason = _call_from(measure.better, difference, interval, margin), None
    if measure.role == "quality" and call != "worse":
        for group in groups:
            if sum(s.n for s in new_g[group]) >= minimum and sum(s.n for s in old_g[group]) >= minimum:
                one = _pooled(new_g, old_g, [group])
                assert one is not None
                interval_g = _interval(new_g, old_g, [group], seed + 1, confidence)
                if _call_from(measure.better, one[0], interval_g, measure.margin(one[1])) == "worse":
                    call, reason = "worse", "harm_in_group"
    return MeasureCall(measure.name, measure.role, call, reason, difference, interval, margin, tuple(groups),
                       n_new, n_old, (baseline_value, new_value))
