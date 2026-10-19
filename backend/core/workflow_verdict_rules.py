"""The baseline, the frozen plan, the looks and the verdict (verdict design §3.2, §3.4, §3.6).

``compute(snapshot)`` is pure: the read phase (``core.workflow_verdict_read``) builds the snapshot
and the caller's write transaction records what it returns (``core.workflow_verdict_evaluator``).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Mapping, Sequence

from core.workflow_verdicts import (
    BLOCKED, BUILTIN_WINDOW_DAYS, CORRECTION_FLOOR, CORRECTIONS, COST, DEFAULT_MINIMUM, MINUTES, MIXED,
    POLICY_VERSION, MeasureCall, PlannedMeasure, Run, measure_call, parse_ts,
)
from infra.measure_validation import deciding_refusal

BUILTINS = (
    PlannedMeasure(CORRECTIONS, "quality", "lower", "builtin", BUILTIN_WINDOW_DAYS, floor=CORRECTION_FLOOR),
    PlannedMeasure(COST, "spend", "lower", "builtin", 0),
    PlannedMeasure(MINUTES, "shown", "lower", "builtin", BUILTIN_WINDOW_DAYS),
)
# Not enough data reasons in the order they take precedence (design §5.2); the first that applies shows.
REASON_ORDER = ("no_shared_groups", "counter_changed", "coverage_under_half", "no_baseline",
                "no_independent_quality_measure", "kpi_unconfirmed")
_INDEPENDENT = {"kpi_engine_missing": "no_independent_quality_measure"}
PLAN_TEXT_LIMIT = 2_000  # infra.outcome_event_attributes: measurement.verdict.planned plan


@dataclass(frozen=True)
class Definition:
    kind: str
    name: str
    version: int
    spec: Mapping[str, object]
    synced_at: str
    removed: bool
    file_path: str


@dataclass(frozen=True)
class WorkflowHistory:
    ref: str
    owner: str | None
    runs: tuple[Run, ...]
    definitions: tuple[Definition, ...] = ()
    version_attr: str | None = None  # not persisted by measure_sync yet, so the read side passes None
    excluded: Mapping[str, int] = field(default_factory=dict)  # never-judged runs, counted: unversioned, local_edit
    first_uses: Mapping[str, str] = field(default_factory=dict)

    def versions(self) -> list[str]:
        """Judged versions by first use (v1, v2, ...), the same first use ``first_use`` reports."""
        return sorted({run.version for run in self.runs}, key=lambda version: (parse_ts(self.first_use(version)), version))

    def first_use(self, version: str) -> str:
        """Earliest use over full history when the reader carried it, else over these runs (calibration splits)."""
        return self.first_uses.get(version) or min(
            (run.started_at for run in self.runs if run.version == version), key=parse_ts)


@dataclass(frozen=True)
class FrozenPlan:
    subject: str
    version: str
    baseline: str | None
    skipped: tuple[str, ...]
    measures: tuple[PlannedMeasure, ...]
    minimum: int
    refusals: tuple[tuple[str, str], ...]
    seed: int
    policy_version: str = POLICY_VERSION

    def encoded(self) -> str:
        """The plan as the ``measurement.verdict.planned`` text; refusals keep only the KPI names."""
        data = {**asdict(self), "measures": [list(asdict(m).values()) for m in self.measures],
                "refusals": [name for name, _why in self.refusals]}
        text = json.dumps(data, sort_keys=True, separators=(",", ":"))
        if len(text) > PLAN_TEXT_LIMIT:
            raise ValueError(f"frozen plan for {self.subject} is {len(text)} characters; the record holds {PLAN_TEXT_LIMIT}")
        return text

    @classmethod
    def decoded(cls, text: str) -> FrozenPlan:
        data = json.loads(text)
        return cls(data["subject"], data["version"], data["baseline"], tuple(data["skipped"]),
                   tuple(PlannedMeasure(*item) for item in data["measures"]), int(data["minimum"]),
                   tuple((name, "") for name in data["refusals"]), int(data["seed"]), data["policy_version"])


@dataclass(frozen=True)
class Verdict:
    word: str  # keep, revert, changed_early (stored); collecting, not_enough_data (computed on read)
    reason: str
    look: int
    calls: tuple[MeasureCall, ...] = ()
    inputs: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Evaluation:
    subject: str
    owner: str | None
    plan: FrozenPlan
    verdict: Verdict
    plan_is_new: bool


@dataclass(frozen=True)
class Snapshot:
    now: str
    workflows: tuple[WorkflowHistory, ...]
    plans: Mapping[str, FrozenPlan] = field(default_factory=dict)
    reached: frozenset[str] = frozenset()


def definitions_in_force(definitions: Sequence[Definition], at: str) -> list[Definition]:
    """Each name's latest synced version at ``at``; a removed one is gone."""
    latest: dict[tuple[str, str], Definition] = {}
    for item in sorted(definitions, key=lambda d: d.version):
        if parse_ts(item.synced_at) <= parse_ts(at):
            latest[(item.kind, item.name)] = item
    return [item for item in latest.values() if not item.removed]


def kpi_window_days(spec: Mapping[str, object]) -> int:
    """The owner-declared KPI follow-up window in whole days."""
    return int(spec.get("settle_days") or 0)  # type: ignore[call-overload]


def assert_evaluator_fields(definitions: Sequence[Definition], workflow: str, version_attr: str | None = None
                            ) -> tuple[tuple[PlannedMeasure, ...], tuple[tuple[str, str], ...], int]:
    """The deciding KPIs that still pass the sync test, the refused ones with why, and the minimum.

    A spend KPI never counts toward the two quality KPIs; a third quality KPI is refused.
    """
    events = {d.name: dict(d.spec) for d in definitions if d.kind == "event"}
    measures, refusals, minimum, quality = [], [], DEFAULT_MINIMUM, 0
    for item in sorted((d for d in definitions if d.kind == "kpi"), key=lambda d: d.name):
        spec = dict(item.spec)
        if spec.get("can_decide") is not True:
            continue
        refusal = deciding_refusal(item.name, spec, events, workflow, version_attr)
        role = str(spec.get("decides_as") or "quality")
        if refusal is None and role == "quality":
            quality += 1
            refusal = f"kpi {item.name} is quality KPI number {quality}; a workflow may have two" if quality > 2 else None
        if refusal is not None:
            refusals.append((item.name, refusal))
            continue
        gap = abs(float(spec["target"]) - float(spec["review"]))  # type: ignore[arg-type]
        measures.append(PlannedMeasure(item.name, role, str(spec["better"]), "kpi", kpi_window_days(spec),  # type: ignore[call-overload]
                                       item.version, margin_gap=gap, coverage=bool(spec.get("coverage"))))
        minimum = max(minimum, int(spec.get("min_n") or 0))  # type: ignore[call-overload]
    return tuple(measures), tuple(refusals), minimum


def _side(history: WorkflowHistory, version: str) -> list[Run]:
    return [run for run in history.runs if run.version == version and run.group[0] != MIXED]


def baseline_runs(history: WorkflowHistory, version: str, minimum: int, window: int, now: datetime) -> list[Run]:
    """The version's most recent settled runs, up to twice the minimum per group."""
    kept: dict[tuple[str, str, str], list[Run]] = {}
    for run in sorted(_side(history, version), key=lambda r: parse_ts(r.terminal_at or r.started_at), reverse=True):
        if run.settled(window, now) and len(kept.setdefault(run.group, [])) < 2 * minimum:
            kept[run.group].append(run)
    return [run for runs in kept.values() for run in runs]


def choose_baseline(history: WorkflowHistory, version: str, minimum: int, window: int, now: datetime
                    ) -> tuple[str | None, tuple[str, ...]]:
    """The nearest earlier version with the minimum settled runs in groups shared with ``version``."""
    order = history.versions()
    groups = {run.group for run in _side(history, version)}
    skipped: list[str] = []
    for candidate in reversed(order[:order.index(version)]):
        if sum(run.group in groups for run in baseline_runs(history, candidate, minimum, window, now)) >= minimum:
            return candidate, tuple(skipped)
        skipped.append(candidate)
    return None, tuple(skipped)


def freeze_plan(history: WorkflowHistory, version: str, *, baseline_as_of: datetime | None = None) -> FrozenPlan:
    """Freeze a plan at first use, or at ``baseline_as_of`` for retrospective calibration (design §3.4)."""
    subject = f"workflow:{history.ref}@{version}"
    in_force = definitions_in_force(history.definitions, history.first_use(version))
    kpis, refusals, minimum = assert_evaluator_fields(in_force, history.ref, history.version_attr)
    measures = (*BUILTINS, *kpis)
    window = max(measure.window_days for measure in measures)
    baseline, skipped = choose_baseline(
        history, version, minimum, window, baseline_as_of or parse_ts(history.first_use(version)))
    seed = int.from_bytes(hashlib.sha256(subject.encode("utf-8")).digest()[:8], "big")
    return FrozenPlan(subject, version, baseline, skipped, measures, minimum, refusals, seed)


def _not_enough(calls: Sequence[MeasureCall], extra: Sequence[str] = ()) -> Verdict | None:
    """Rule 5 and the unconfirmed-rows rule: Not enough data, naming the first reason that applies."""
    quality = [call for call in calls if call.role == "quality"]
    reasons = list(extra)
    if not [call for call in quality if call.call not in BLOCKED]:
        reasons += [_INDEPENDENT.get(call.reason or "", call.reason or "") for call in quality] or ["no_independent_quality_measure"]
    unconfirmed = [call for call in quality if call.call == "unconfirmed"]
    reasons += ["kpi_unconfirmed"] if unconfirmed else []
    known = [reason for reason in REASON_ORDER if reason in reasons]
    if not known:
        return None
    inputs = dict(unconfirmed[0].detail) if known[0] == "kpi_unconfirmed" else {}
    return Verdict("not_enough_data", known[0], 0, tuple(calls), inputs)


def _rule(calls: Sequence[MeasureCall], look: int) -> Verdict | None:
    """Rules 1 to 4 at a look; None means wait for look 2."""
    quality = [c for c in calls if c.role == "quality" and c.call not in BLOCKED]
    spend = [c for c in calls if c.role == "spend"]
    worse = [c.name for c in quality if c.call == "worse"]
    better = [c.name for c in quality if c.call == "better"]
    costly = [c.name for c in spend if c.call == "worse"]
    if worse:
        reason = "quality_disagree" if better else "quality_worse"
        return Verdict("revert", reason, look, tuple(calls), {"worse": worse, "better": better})
    if better:
        return Verdict("keep", "quality_better", look, tuple(calls), {"better": better, "cost_worse": costly})
    if all(c.call == "no_worse" for c in quality):
        if costly:
            return Verdict("revert", "cost_worse_no_gain", look, tuple(calls), {"cost_worse": costly})
        cheaper = any(c.call == "better" for c in spend)
        return Verdict("keep", "no_worse", look, tuple(calls), {"cheaper": cheaper})
    return Verdict("keep", "no_clear_difference", look, tuple(calls)) if look == 2 else None


def _prefix(plan: FrozenPlan, deciding: Sequence[PlannedMeasure], new: Sequence[Run], old: Sequence[Run],
            target: int, now: datetime) -> int | None:
    """The fewest earliest-settled new runs at which every deciding measure has ``target`` new runs and the
    minimum baseline runs in eligible groups. Looking only at that fixed prefix keeps a look's verdict the
    same however many runs arrive after it."""
    for length in range(1, len(new) + 1):
        counts = [measure_call(m, new[:length], old, minimum=target, now=now, seed=plan.seed, bootstrap=False,
                               base_minimum=plan.minimum) for m in deciding]
        if all(c.call == "counted" for c in counts):
            return length
    return None


def decide(history: WorkflowHistory, plan: FrozenPlan, now: datetime, replaced_by: str | None = None) -> Verdict:
    """Collecting, Changed again, Not enough data, or the verdict at look 1 or look 2."""
    if plan.baseline is None:
        return Verdict("not_enough_data", "no_baseline", 0, (), {"minimum": plan.minimum, "skipped": list(plan.skipped)})
    window = max(m.window_days for m in plan.measures)
    old = baseline_runs(history, plan.baseline, plan.minimum, window, now)
    new = sorted((r for r in _side(history, plan.version) if r.terminal_at), key=lambda r: parse_ts(r.terminal_at or ""))
    call = lambda m, runs, bootstrap=True: measure_call(m, runs, old, minimum=plan.minimum, now=now,  # noqa: E731
                                                        seed=plan.seed + len(m.name), bootstrap=bootstrap)
    overall = [call(m, new, bootstrap=False) for m in plan.measures]
    blocked = _not_enough(overall)
    if blocked is not None:
        return blocked
    deciding = [m for m, c in zip(plan.measures, overall) if m.role != "shown" and c.call not in BLOCKED | {"unknown"}]
    first = _prefix(plan, deciding, new, old, plan.minimum, now)
    inputs = {"baseline": plan.baseline, "skipped": list(plan.skipped)}
    if replaced_by is not None:
        cutoff, own = parse_ts(history.first_use(replaced_by)), _side(history, plan.version)
        reached = first is not None and max(parse_ts(r.started_at) for r in new[:first]) < cutoff
        pending = any(not r.settled(window, now) for r in own if parse_ts(r.started_at) < cutoff)
        if not reached and not pending:
            k = sum(1 for r in own if parse_ts(r.started_at) < cutoff)
            return Verdict("changed_early", "replaced_before_look_1", 0, (),
                           {**inputs, "version": plan.version, "k": k, "next": replaced_by})
    if first is None:
        counted = [c for c in overall if c.role != "shown" and c.call not in BLOCKED | {"unknown"}]
        groups = sorted({g for c in counted for g in c.groups})
        return Verdict("collecting", "below_minimum", 0, tuple(counted),
                       {**inputs, "n": min((c.n_new for c in counted), default=0), "minimum": plan.minimum,
                        "groups": [list(g) for g in groups]})
    verdict = _rule([call(m, new[:first]) for m in plan.measures], 1)
    if verdict is not None:
        return Verdict(verdict.word, verdict.reason, 1, verdict.calls, {**inputs, **verdict.inputs})
    second = _prefix(plan, deciding, new, old, 2 * plan.minimum, now)
    if second is None:
        return Verdict("collecting", "wait_for_look_2", 1, (), {**inputs, "minimum": 2 * plan.minimum})
    final = _rule([call(m, new[:second]) for m in plan.measures], 2)
    assert final is not None
    return Verdict(final.word, final.reason, 2, final.calls, {**inputs, **final.inputs})


def compute(snapshot: Snapshot) -> list[Evaluation]:
    """Every judged version's evaluation; a version with a reached verdict is never recomputed."""
    now, evaluations = parse_ts(snapshot.now), []
    for history in snapshot.workflows:
        order = history.versions()
        for index, version in enumerate(order):
            subject = f"workflow:{history.ref}@{version}"
            if subject in snapshot.reached:
                continue
            existing = snapshot.plans.get(subject)
            plan = existing or freeze_plan(history, version)
            replaced_by = order[index + 1] if index + 1 < len(order) else None
            evaluations.append(Evaluation(subject, history.owner, plan, decide(history, plan, now, replaced_by),
                                          existing is None))
    return evaluations
