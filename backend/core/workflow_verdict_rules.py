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
    DEFAULT_CONFIDENCE, POLICY_VERSION, RAISED_CONFIDENCES, MeasureCall, PlannedMeasure, Run, measure_call,
    parse_ts,
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
    verdict_confidence: float = DEFAULT_CONFIDENCE
    verdict_confidence_reason: str | None = None

    def _in_start_order(self) -> list[Run]:
        return sorted(self.runs, key=lambda item: (parse_ts(item.started_at), item.run_id))

    def versions(self) -> list[str]:
        """Judged versions by first use (v1, v2, ...)."""
        first: dict[str, str] = {}
        for run in self._in_start_order():
            first.setdefault(run.version, run.started_at)
        return list(first)

    def first_use(self, version: str) -> str:
        return min((run.started_at for run in self.runs if run.version == version), key=parse_ts)

    def successor(self, version: str) -> tuple[str, str] | None:
        """The version that replaced ``version`` and when: the first other version to run after its last run,
        whether newer or rolled back to; None while it is still the one in use."""
        ordered = self._in_start_order()
        last = max(i for i, run in enumerate(ordered) if run.version == version)
        after = next((run for run in ordered[last + 1:] if run.version != version), None)
        return (after.version, after.started_at) if after is not None else None

    def predecessors(self, version: str) -> list[str]:
        """The other versions that ran before ``version``'s first use, latest run first: the one it replaced,
        then the one before that. The reverse of ``successor``: after v1, v2, back to v1, then v3, v3 replaced
        v1, so v1 comes before v2."""
        ordered = self._in_start_order()
        first = next(i for i, run in enumerate(ordered) if run.version == version)
        return list(dict.fromkeys(run.version for run in reversed(ordered[:first])))


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
    confidence: float = DEFAULT_CONFIDENCE
    confidence_reason: str | None = None

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
                   tuple((name, "") for name in data["refusals"]), int(data["seed"]), data["policy_version"],
                   float(data.get("confidence", DEFAULT_CONFIDENCE)), data.get("confidence_reason"))


@dataclass(frozen=True)
class Verdict:
    word: str  # keep, revert (stored); changed_early, collecting, not_enough_data (computed on read)
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
class TwoArms:
    """A neutral comparison input: the current/control arm and the changed arm."""

    control: tuple[Run, ...]
    changed: tuple[Run, ...]
    continue_if_unclear: bool = True


@dataclass(frozen=True)
class Snapshot:
    now: str
    workflows: tuple[WorkflowHistory, ...]
    plans: Mapping[str, FrozenPlan] = field(default_factory=dict)
    reached: frozenset[str] = frozenset()
    subjects: frozenset[str] | None = None  # None: all versions; set: this bounded open batch


def definitions_in_force(definitions: Sequence[Definition], at: str) -> list[Definition]:
    """Each name's latest synced version at ``at``; a removed one is gone."""
    latest: dict[tuple[str, str], Definition] = {}
    for item in sorted(definitions, key=lambda d: d.version):
        if parse_ts(item.synced_at) <= parse_ts(at):
            latest[(item.kind, item.name)] = item
    return [item for item in latest.values() if not item.removed]


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
        measures.append(PlannedMeasure(item.name, role, str(spec["better"]), "kpi", int(spec.get("settle_days") or 0),  # type: ignore[call-overload]
                                       item.version, margin_gap=gap, coverage=bool(spec.get("coverage")),
                                       guard_against_harm=spec.get("guard_against_harm") is True))
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
    """The version ``version`` replaced, or failing that the latest-run earlier version, with the minimum
    settled runs at ``now``. ``version``'s own groups are unknown at first use, so shared groups are left to
    the comparison (``no_shared_groups``)."""
    skipped: list[str] = []
    for candidate in history.predecessors(version):
        if len(baseline_runs(history, candidate, minimum, window, now)) >= minimum:
            return candidate, tuple(skipped)
        skipped.append(candidate)
    return None, tuple(skipped)


def _validated_confidence(history: WorkflowHistory) -> tuple[float, str | None]:
    """Accept only the default confidence or an explicitly justified approved elevation."""
    confidence, reason = history.verdict_confidence, history.verdict_confidence_reason
    if confidence == DEFAULT_CONFIDENCE and reason is None:
        return confidence, None
    if confidence in RAISED_CONFIDENCES and isinstance(reason, str) and reason.strip():
        return confidence, reason
    raise ValueError("verdict confidence must be the default or an approved raised value with a reason")


def freeze_plan(history: WorkflowHistory, version: str) -> FrozenPlan:
    """Freeze definitions, baseline eligibility, and validated confidence at first use."""
    subject = f"workflow:{history.ref}@{version}"
    started = history.first_use(version)
    first_use = parse_ts(started)
    in_force = definitions_in_force(history.definitions, started)
    kpis, refusals, minimum = assert_evaluator_fields(in_force, history.ref, history.version_attr)
    measures = (*BUILTINS, *kpis)
    window = max(measure.window_days for measure in measures)
    baseline, skipped = choose_baseline(history, version, minimum, window, first_use)
    seed = int.from_bytes(hashlib.sha256(subject.encode("utf-8")).digest()[:8], "big")
    confidence, confidence_reason = _validated_confidence(history)
    return FrozenPlan(subject, version, baseline, skipped, measures, minimum, refusals, seed,
                      confidence=confidence, confidence_reason=confidence_reason)


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


def _near_zero(call: MeasureCall) -> bool:
    """A neutral result has no directional interval and stays inside the measure's declared margin."""
    if call.difference is None or call.interval is None or call.margin is None:
        return False
    return call.interval[0] <= 0 <= call.interval[1] and abs(call.difference) < call.margin


def _rule(calls: Sequence[MeasureCall], measures: Sequence[PlannedMeasure], look: int) -> Verdict | None:
    """Rules 1 to 4 at a look; a final unclear guard retains the current arm."""
    quality = [c for c in calls if c.role == "quality" and c.call not in BLOCKED]
    spend = [c for c in calls if c.role == "spend"]
    worse = [c.name for c in quality if c.call == "worse"]
    better = [c.name for c in quality if c.call == "better"]
    costly = [c.name for c in spend if c.call == "worse"]
    guards = {measure.name for measure in measures if measure.guard_against_harm}
    if worse:
        reason = "quality_disagree" if better else "quality_worse"
        return Verdict("revert", reason, look, tuple(calls), {"worse": worse, "better": better})
    if better:
        return Verdict("keep", "quality_better", look, tuple(calls), {"better": better, "cost_worse": costly})
    if all(c.call == "no_worse" for c in quality):
        if costly:
            return Verdict("revert", "cost_worse_no_gain", look, tuple(calls), {"cost_worse": costly})
        cheaper = any(c.call == "better" for c in spend)
        if cheaper and not all(_near_zero(c) for c in quality):
            return Verdict("revert", "cheaper_not_near_zero", look, tuple(calls))
        return Verdict("keep", "no_worse", look, tuple(calls), {"cheaper": cheaper})
    guarded = sorted({c.name for c in quality if c.name in guards and c.call == "unclear"})
    if look == 3:
        if guarded:
            return Verdict("revert", "guard_unclear", look, tuple(calls), {"guards": guarded})
        return Verdict("keep", "no_clear_difference", look, tuple(calls))
    return None


def _prefix(plan: FrozenPlan, deciding: Sequence[PlannedMeasure], changed: Sequence[Run], control: Sequence[Run],
            target: int, now: datetime) -> int | None:
    """The fewest changed-arm runs at which every deciding measure reaches ``target`` in its own denominator."""
    for length in range(1, len(changed) + 1):
        counts = [measure_call(m, changed[:length], control, minimum=target, now=now, seed=plan.seed,
                               confidence=plan.confidence, bootstrap=False, base_minimum=plan.minimum) for m in deciding]
        if all(c.call == "counted" for c in counts):
            return length
    return None


def sides(history: WorkflowHistory, plan: FrozenPlan) -> tuple[list[Run], list[Run]]:
    """The baseline runs settled by the version's first use, and the version's own ended runs in end order."""
    assert plan.baseline is not None
    window = max(m.window_days for m in plan.measures)
    old = baseline_runs(history, plan.baseline, plan.minimum, window, parse_ts(history.first_use(plan.version)))
    new = sorted((r for r in _side(history, plan.version) if r.terminal_at), key=lambda r: parse_ts(r.terminal_at or ""))
    return old, new


def decide_arms(plan: FrozenPlan, arms: TwoArms, now: datetime, *, inputs: Mapping[str, object] | None = None) -> Verdict:
    """Judge generic control and changed arms; callers own how they select or name those arms."""
    control, changed = list(arms.control), list(arms.changed)
    call = lambda measure, runs, bootstrap=True: measure_call(  # noqa: E731
        measure, runs, control, minimum=plan.minimum, now=now, seed=plan.seed + len(measure.name),
        confidence=plan.confidence, bootstrap=bootstrap,
    )
    overall = [call(measure, changed, bootstrap=False) for measure in plan.measures]
    blocked = _not_enough(overall)
    if blocked is not None:
        return blocked
    deciding = [measure for measure, result in zip(plan.measures, overall)
                if measure.role != "shown" and result.call not in BLOCKED | {"unknown"}]
    context = dict(inputs or {})
    first = _prefix(plan, deciding, changed, control, plan.minimum, now)
    if first is None:
        counted = [result for result in overall if result.role != "shown" and result.call not in BLOCKED | {"unknown"}]
        groups = sorted({group for result in counted for group in result.groups})
        return Verdict("collecting", "below_minimum", 0, tuple(counted),
                       {**context, "n": min((result.n_new for result in counted), default=0),
                        "minimum": plan.minimum, "groups": [list(group) for group in groups]})
    verdict = _rule([call(measure, changed[:first]) for measure in plan.measures], plan.measures, 1)
    if verdict is not None:
        return Verdict(verdict.word, verdict.reason, 1, verdict.calls, {**context, **verdict.inputs})
    second = _prefix(plan, deciding, changed, control, 2 * plan.minimum, now)
    if second is None:
        return Verdict("collecting", "wait_for_look_2", 1, (), {**context, "minimum": 2 * plan.minimum})
    verdict = _rule([call(measure, changed[:second]) for measure in plan.measures], plan.measures, 2)
    if verdict is not None:
        return Verdict(verdict.word, verdict.reason, 2, verdict.calls, {**context, **verdict.inputs})
    if not arms.continue_if_unclear:
        return Verdict("revert", "queued_after_look_2", 2, (), context)
    third = _prefix(plan, deciding, changed, control, 4 * plan.minimum, now)
    if third is None:
        return Verdict("collecting", "wait_for_look_3", 2, (), {**context, "minimum": 4 * plan.minimum})
    final = _rule([call(measure, changed[:third]) for measure in plan.measures], plan.measures, 3)
    assert final is not None
    return Verdict(final.word, final.reason, 3, final.calls, {**context, **final.inputs})


def decide(history: WorkflowHistory, plan: FrozenPlan, now: datetime,
           replaced_by: tuple[str, str] | None = None) -> Verdict:
    """Adapt the workflow-version history into the generic two-arm verdict rule."""
    if plan.baseline is None:
        return Verdict("not_enough_data", "no_baseline", 0, (), {"minimum": plan.minimum, "skipped": list(plan.skipped)})
    window = max(measure.window_days for measure in plan.measures)
    control, changed = sides(history, plan)
    inputs = {"baseline": plan.baseline, "skipped": list(plan.skipped)}
    if replaced_by is not None and all(run.settled(window, now) for run in _side(history, plan.version)):
        reached = decide_arms(plan, TwoArms(tuple(control), tuple(changed)), now, inputs=inputs)
        if reached.word == "collecting" and reached.reason == "below_minimum":
            return Verdict("changed_early", "replaced_before_look_1", 0, (),
                           {**inputs, "version": plan.version, "k": len(changed), "next": replaced_by[0]})
        return reached
    return decide_arms(plan, TwoArms(tuple(control), tuple(changed)), now, inputs=inputs)


def compute(snapshot: Snapshot) -> list[Evaluation]:
    """Every judged version's evaluation; a version with a reached verdict is never recomputed."""
    now, evaluations = parse_ts(snapshot.now), []
    for history in snapshot.workflows:
        for version in history.versions():
            subject = f"workflow:{history.ref}@{version}"
            if (snapshot.subjects is not None and subject not in snapshot.subjects) or subject in snapshot.reached:
                continue
            existing = snapshot.plans.get(subject)
            plan = existing or freeze_plan(history, version)
            replaced_by = history.successor(version)
            evaluations.append(Evaluation(subject, history.owner, plan, decide(history, plan, now, replaced_by),
                                          existing is None))
    return evaluations
