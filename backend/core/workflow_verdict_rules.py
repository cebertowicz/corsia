"""The baseline, the frozen plan, the looks and the verdict (verdict design §3.2, §3.4, §3.6).

``compute(snapshot)`` is pure: the read phase (``core.workflow_verdict_read``) builds the snapshot
and the caller's write transaction records what it returns (``core.workflow_verdict_evaluator``).
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Mapping, Sequence

from core.workflow_verdicts import (
    BLOCKED, BUILTIN_WINDOW_DAYS, CORRECTION_FLOOR, CORRECTIONS, COST, DEFAULT_MINIMUM, MINUTES, MIXED,
    DEFAULT_CONFIDENCE, RAISED_CONFIDENCES, SMALL_HARM, MeasureCall, PlannedMeasure, Run, measure_call,
    parse_ts,
)
from core.workflow_verdict_looks import LOOK_MULTIPLES, look_confidence, planned_mdc
from core.workflow_verdict_types import (
    Definition, Evaluation, FrozenPlan, Snapshot, TwoArms, Verdict, WorkflowHistory, subject_of,
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
    subject = subject_of("workflow", history.ref, version)
    started = history.first_use(version)
    first_use = parse_ts(started)
    in_force = definitions_in_force(history.definitions, started)
    kpis, refusals, minimum = assert_evaluator_fields(in_force, history.ref, history.version_attr)
    measures = (*BUILTINS, *kpis)
    window = max(measure.window_days for measure in measures)
    baseline, skipped = choose_baseline(history, version, minimum, window, first_use)
    seed = int.from_bytes(hashlib.sha256(subject.encode("utf-8")).digest()[:8], "big")
    confidence, confidence_reason = _validated_confidence(history)
    arm = [] if baseline is None else baseline_runs(history, baseline, minimum, window, first_use)
    return FrozenPlan(subject, version, baseline, skipped, measures, minimum, refusals, seed,
                      confidence=confidence, confidence_reason=confidence_reason,
                      mdc=planned_mdc(measures, arm, minimum, confidence, first_use))


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
    """Apply the keep-only-for-improvement-or-cheaper rule at one look."""
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
        if not cheaper:
            return Verdict("revert", "no_gain_not_cheaper", look, tuple(calls))
        if any(c.reason == SMALL_HARM for c in quality):
            return Verdict("revert", "cheaper_small_harm", look, tuple(calls))
        return Verdict("keep", "no_worse", look, tuple(calls), {"cheaper": True})
    # Mental model, "How it's decided": an unclear result keeps the current version, never the cheaper one.
    if look == 3:
        return Verdict("revert", "unclear_after_look_3", look, tuple(calls))
    return None


def _prefix(plan: FrozenPlan, deciding: Sequence[PlannedMeasure], changed: Sequence[Run], control: Sequence[Run],
            target: int, now: datetime, *, matched: bool = False) -> int | None:
    """The fewest changed-arm runs at which every deciding measure reaches ``target`` in its own denominator.

    A fixed baseline needs only the plan's minimum; ``matched`` halves both need ``target``."""
    base = target if matched else plan.minimum
    for length in range(1, len(changed) + 1):
        counts = [measure_call(m, changed[:length], control, minimum=target, now=now, seed=plan.seed,
                               confidence=plan.confidence, bootstrap=False, base_minimum=base) for m in deciding]
        if all(c.call == "counted" for c in counts):
            return length
    return None


def _cut(plan: FrozenPlan, deciding: Sequence[PlannedMeasure], arms: TwoArms, target: int, now: datetime
         ) -> tuple[list[Run], list[Run]] | None:
    """The arms as one look sees them, so a look's verdict is the same however many runs arrive after it.

    A baseline is fixed, so only the changed arm is cut. Matched halves grow together (mental model, "When":
    20 tasks a half, then 40), so each is cut to the fewest earliest-settled runs that reach ``target``."""
    control, changed = list(arms.control), list(arms.changed)
    if not arms.matched:
        length = _prefix(plan, deciding, changed, control, target, now)
        return None if length is None else (control, changed[:length])
    kept = _prefix(plan, deciding, control, changed, target, now, matched=True)
    if kept is None:
        return None
    length = _prefix(plan, deciding, changed, control[:kept], target, now, matched=True)
    return None if length is None else (control[:kept], changed[:length])


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

    def call(measure: PlannedMeasure, runs: Sequence[Run], against: Sequence[Run] = control, bootstrap: bool = True,
             look: int = 1) -> MeasureCall:
        return measure_call(measure, runs, against, minimum=plan.minimum, now=now, seed=plan.seed + len(measure.name),
                            confidence=look_confidence(plan.confidence, look), bootstrap=bootstrap)

    overall = [call(measure, changed, bootstrap=False) for measure in plan.measures]
    blocked = _not_enough(overall)
    if blocked is not None:
        return blocked
    deciding = [measure for measure, result in zip(plan.measures, overall)
                if measure.role != "shown" and result.call not in BLOCKED | {"unknown"}]
    context = dict(inputs or {})

    def rule_at(look: int) -> tuple[Verdict | None, bool]:
        """The rule at a look, judged at that look's confidence, and whether the arms reached it."""
        cut = _cut(plan, deciding, arms, LOOK_MULTIPLES[look] * plan.minimum, now)
        if cut is None:
            return None, False
        return _rule([call(measure, cut[1], cut[0], look=look) for measure in plan.measures], look), True

    first, reached = rule_at(1)
    if not reached:
        counted = [result for result in overall if result.role != "shown" and result.call not in BLOCKED | {"unknown"}]
        groups = sorted({group for result in counted for group in result.groups})
        return Verdict("collecting", "below_minimum", 0, tuple(counted),
                       {**context, "n": min((result.n_new for result in counted), default=0),
                        "minimum": plan.minimum, "groups": [list(group) for group in groups]})
    if first is not None:
        return Verdict(first.word, first.reason, 1, first.calls, {**context, **first.inputs})
    second, reached = rule_at(2)
    if not reached:
        return Verdict("collecting", "wait_for_look_2", 1, (), {**context, "minimum": 2 * plan.minimum})
    if second is not None:
        return Verdict(second.word, second.reason, 2, second.calls, {**context, **second.inputs})
    if not arms.continue_if_unclear:
        return Verdict("revert", "queued_after_look_2", 2, (), context)
    final, reached = rule_at(3)
    if not reached:
        return Verdict("collecting", "wait_for_look_3", 2, (), {**context, "minimum": 4 * plan.minimum})
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
            subject = subject_of("workflow", history.ref, version)
            if (snapshot.subjects is not None and subject not in snapshot.subjects) or subject in snapshot.reached:
                continue
            existing = snapshot.plans.get(subject)
            plan = existing or freeze_plan(history, version)
            replaced_by = history.successor(version)
            evaluations.append(Evaluation(subject, history.owner, plan, decide(history, plan, now, replaced_by),
                                          existing is None))
    return evaluations
