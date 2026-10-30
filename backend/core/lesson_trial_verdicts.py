"""Model-free verdicts for lesson trials: the with and without halves judged by the shared two-arm rule.

Source: the learning-loop mental model, "Side by side: with and without a lesson" and "What happens to a
lesson". The unit is the plan task. Each task a trial was delivered to is one ``Run`` whose version is the half
it was drawn into (``with`` or ``without``) and whose group is its workflow step, so the existing within-group
pooling does the step split. Tasks that crossed over and tasks the trial skipped (omitted) belong to neither
half. ``core.workflow_verdict_rules.decide_arms`` makes the call; this module only selects the arms, freezes
the plan, finds the owner and reads back what the nightly job needs.

Reads only. ``core.workflow_verdict_evaluator.write_verdicts`` records inside the job's write transaction.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from loguru import logger

from api.measurement_queries import RESTART_REASONS_SQL
from core.context_source_paths import project_id_for_root
from core.lesson_files import TrialFile, read_trial
from core.workflow_step_binding import declared_binding
from core.workflow_verdict_looks import planned_mdc
from core.workflow_verdict_read import (
    OPEN_BATCH_LIMIT, REACHED_EVENT, json_ids, node_terminal_at, price_snapshot, recorded_verdicts, terminal_times,
    workflow_definitions,
)
from core.workflow_verdict_rules import (
    Definition, Evaluation, FrozenPlan, Snapshot, TwoArms, WorkflowHistory, assert_evaluator_fields, decide_arms,
    definitions_in_force,
)
from core.workflow_verdict_types import subject_of
from core.workflow_verdicts import (
    CORRECTION_FLOOR, COST, DEFAULT_CONFIDENCE, DEFAULT_MINIMUM, KpiRow, PlannedMeasure, Run, parse_ts,
)
from core.workspace_layout import thread_tray_owners
from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator
from tools.filesystem_gate import join_and_resolve

SENT_BACK, RESTARTED = "sent_back", "restarted"
OBSERVED = "observed"  # both measures are read straight from the plan: a row fact, not an owner's label
WITH, WITHOUT = "with", "without"
NO_STEP = "none"
# Open trials: a delivered (lesson, version) with no reached verdict yet. Whether the trial file still says
# `trial` at that version is confirmed against the file, so a retired or edited lesson drops out.
OPEN_TRIALS_SQL = """
    SELECT g.lesson_id, g.lesson_behavioral_sha256 AS sha, MIN(g.created_at) AS first_at,
      (SELECT p.path FROM guidance_loads p WHERE p.lesson_id=g.lesson_id
         AND p.lesson_behavioral_sha256=g.lesson_behavioral_sha256 ORDER BY p.created_at DESC, p.id DESC LIMIT 1) AS path
    FROM guidance_loads g
    WHERE g.lesson_id IS NOT NULL AND g.lesson_behavioral_sha256 IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM outcome_events r WHERE r.event_name=:reached AND r.retracted_at IS NULL
          AND r.subject_key='lesson:'||g.lesson_id||'@'||g.lesson_behavioral_sha256)
    GROUP BY g.lesson_id, g.lesson_behavioral_sha256
    ORDER BY first_at, g.lesson_id
"""
ARM_ROWS_SQL = """
    SELECT g.node_id, g.thread_id, g.variant, g.created_at AS received_at, n.state, n.created_at, n.updated_at,
      n.workflow_step, n.sent_back
    FROM guidance_loads g LEFT JOIN plan_nodes n ON n.id=g.node_id
    WHERE g.lesson_id=:lesson AND g.lesson_behavioral_sha256=:sha AND g.node_id IS NOT NULL
"""
# A without-half task that opened the lesson anyway: a mid-task read of the trial's own file, in the thread that
# took the receipt or in the worker the task was handed to (a parent can start a step and hand it on).
OPENED_SQL = """
    SELECT r.node_id, m.created_at FROM guidance_loads r
    LEFT JOIN plan_nodes n ON n.id=r.node_id
    JOIN guidance_loads m ON m.thread_id IN (r.thread_id, n.spawned_thread_id) AND m.path=r.path
      AND m.lesson_id IS NULL AND m.created_at>=r.created_at
    WHERE r.lesson_id=:lesson AND r.lesson_behavioral_sha256=:sha AND r.variant='without'
"""
RESTARTED_SQL = f"""
    SELECT DISTINCT node_id FROM plan_node_attempts
    WHERE release_reason IN ({RESTART_REASONS_SQL}) AND node_id IN (SELECT value FROM json_each(:ids))
"""


@dataclass(frozen=True)
class OpenTrial:
    lesson_id: str
    sha: str
    path: Path
    first_at: str
    trial: TrialFile

    @property
    def subject(self) -> str:
        return subject_of("lesson", self.lesson_id, self.sha)


@dataclass(frozen=True)
class Arms:
    """A trial's two halves and what was left out of both."""

    with_runs: tuple[Run, ...]
    without_runs: tuple[Run, ...]
    crossed_over: int
    omitted: int

    def inputs(self) -> dict[str, object]:
        return {"with": len(self.with_runs), "without": len(self.without_runs),
                "crossed_over": self.crossed_over, "omitted": self.omitted}


def open_trials(db: sqlite3.Connection, *, limit: int = OPEN_BATCH_LIMIT) -> list[OpenTrial]:
    """Delivered trial versions without a reached verdict whose file is still that version in state ``trial``.

    The oldest first, stopping once ``limit`` are confirmed: a pair whose file was retired or edited never gets
    a reached verdict, so it is skipped here and must not hold one of the ``limit`` places.
    """
    found: list[OpenTrial] = []
    for row in db.execute(OPEN_TRIALS_SQL, {"reached": REACHED_EVENT}):
        try:
            trial = read_trial(Path(row["path"]).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as error:
            logger.warning("lesson trial {} at {} skipped, unreadable file {}: {}", row["lesson_id"], row["sha"],
                           row["path"], error)
            continue
        if (trial is not None and trial.lesson.id == row["lesson_id"] and trial.lesson.state == "trial"
                and trial.behavioral_sha256 == row["sha"]):
            found.append(OpenTrial(row["lesson_id"], row["sha"], Path(row["path"]), row["first_at"], trial))
            if len(found) == limit:
                break
    return found


def _kpi_rows(sent_back: bool, restarted: bool, at: str) -> dict[str, tuple[KpiRow, ...]]:
    """One numerator and one denominator row per measure; both are final when the task ends."""
    def pair(value: bool) -> tuple[KpiRow, ...]:
        return KpiRow("num", OBSERVED, float(value), at), KpiRow("den", OBSERVED, 1.0, at)

    return {SENT_BACK: pair(sent_back), RESTARTED: pair(restarted)}


def read_arms(db: sqlite3.Connection, open_trial: OpenTrial) -> Arms:
    """Each delivered task as a run in its half; crossed-over and omitted tasks are counted and left out."""
    params = {"lesson": open_trial.lesson_id, "sha": open_trial.sha}
    rows = [dict(row) for row in db.execute(ARM_ROWS_SQL, params).fetchall()]
    ids = [row["node_id"] for row in rows if row["variant"] in (WITH, WITHOUT) and row["state"]]
    times = terminal_times(db, ids)
    restarted = {row["node_id"] for row in db.execute(RESTARTED_SQL, {"ids": json_ids(ids)}).fetchall()}
    opened: dict[str, list[str]] = defaultdict(list)
    for row in db.execute(OPENED_SQL, params).fetchall():
        opened[row["node_id"]].append(row["created_at"])
    halves: dict[str, list[Run]] = {WITH: [], WITHOUT: []}
    crossed = omitted = 0
    for row in rows:
        node = {"id": row["node_id"], "state": row["state"], "updated_at": row["updated_at"]}
        end = node_terminal_at(node, times) if row["state"] else None
        if row["variant"] == "crossover" or (row["variant"] == WITHOUT and any(
                end is None or parse_ts(at) <= parse_ts(end) for at in opened.get(row["node_id"], ()))):
            crossed += 1
        elif row["variant"] not in (WITH, WITHOUT) or not row["state"]:
            omitted += 1
        else:
            facts = _kpi_rows(bool(row["sent_back"]), row["node_id"] in restarted, end) if end else {}
            group = (row["workflow_step"] or NO_STEP, NO_STEP, NO_STEP)
            halves[row["variant"]].append(Run(row["node_id"], row["variant"], group, row["created_at"], end,
                                              row["state"], kpi_rows=facts))
    return Arms(_settled_first(halves[WITH]), _settled_first(halves[WITHOUT]), crossed, omitted)


def _settled_first(runs: Sequence[Run]) -> tuple[Run, ...]:
    """Ended runs in end order, then open ones, as ``sides`` orders a workflow: a look takes the earliest settled."""
    return tuple(sorted(runs, key=lambda run: (run.terminal_at is None, parse_ts(run.terminal_at or run.started_at),
                                               run.run_id)))


def freeze_plan(open_trial: OpenTrial, definitions: Sequence[Definition], workflow: str | None,
                without: Sequence[Run], now: datetime) -> FrozenPlan:
    """The plan fixed at the trial's first use: the measures, the minimum per half, the seed and the minimum
    detectable change from the without half as it stands (often empty, then None).

    Mental model, "What decides one lesson": the workflow owner's declared deciding measures (at most two,
    fixed before the trial starts) decide when there are any; otherwise sent back and finished without a
    restart decide. Cost only breaks ties. Corrections are too rare to decide one trial, so they are not here.
    Finished without a restart is read as its complement, the restarted share, so its margin is a quarter of
    the share that went wrong, as for sent back, not of a share near 1 (DECISION-LOG 2026-10-30).
    """
    declared: tuple[PlannedMeasure, ...] = ()
    refusals, minimum = (), DEFAULT_MINIMUM
    if workflow is not None:
        declared, refusals, minimum = assert_evaluator_fields(
            definitions_in_force(definitions, open_trial.first_at), workflow)
    # Declared KPIs decide only when one of them is a quality measure; a lone spend KPI decides nothing.
    role = "shown" if any(measure.role == "quality" for measure in declared) else "quality"
    measures = (*declared,
                PlannedMeasure(SENT_BACK, role, "lower", "kpi", 0, floor=CORRECTION_FLOOR),
                PlannedMeasure(RESTARTED, role, "lower", "kpi", 0, floor=CORRECTION_FLOOR),
                PlannedMeasure(COST, "spend", "lower", "builtin", 0))
    subject = open_trial.subject
    seed = int.from_bytes(hashlib.sha256(subject.encode("utf-8")).digest()[:8], "big")
    # Confidence stays at the default: no CORSIA metadata key for a raised level exists yet to read.
    return FrozenPlan(subject, open_trial.sha, WITHOUT, (), measures, minimum, refusals, seed,
                      mdc=planned_mdc(measures, without, minimum, DEFAULT_CONFIDENCE, now))


def trial_owner(db: sqlite3.Connection, open_trial: OpenTrial) -> str | None:
    """Who is woken: the owner of the file the lesson changes, never the lesson's author as such.

    A file in a thread's tray belongs to that thread; a project file to the project's conductor; any other
    workspace file to the CORSIA agent. A lesson with no target is its own new file, in its owner's tray.
    """
    from infra.config import WORKSPACE

    workspace = Path(WORKSPACE).expanduser().resolve(strict=False)
    metadata = open_trial.trial.lesson.case_metadata
    target = str(metadata.get("new_target_path") or metadata.get("target_file") or "")
    own = open_trial.path.resolve(strict=False)
    try:
        path = join_and_resolve(target, workspace=workspace) if target else own
    except (OSError, ValueError):
        path = own
    if not path.is_relative_to(workspace):
        path = own
    owners = thread_tray_owners(path)
    if len(owners) == 1:
        return owners[0]
    project = _project_of(path, workspace, db)
    return get_scope_coordinator(project or WORKSPACE_SCOPE, db=db)


def _project_of(path: Path, workspace: Path, db: sqlite3.Connection) -> str | None:
    """The project whose planning directory holds ``path`` (a lexical check), or None outside every project."""
    try:
        relative = path.relative_to(workspace / "projects")
    except ValueError:
        return None
    return project_id_for_root(workspace / "projects" / relative.parts[0], workspace, db) if relative.parts else None


def evaluate_trials(db: sqlite3.Connection, now: datetime, *, limit: int = OPEN_BATCH_LIMIT) -> list[Evaluation]:
    """Every open trial's evaluation, with the cost of each task priced; nothing is written."""
    opened = open_trials(db, limit=limit)
    if not opened:
        return []
    plans, reached = recorded_verdicts(db, [item.subject for item in opened])
    definitions, _owners = workflow_definitions(db)
    arms = {item.subject: read_arms(db, item) for item in opened}
    priced = _priced(db, now, arms)
    evaluations = []
    for item in opened:
        if item.subject in reached:
            continue
        binding, _in_skills = declared_binding(item.trial, {})
        workflow = None if binding is None else binding.workflow
        existing = plans.get(item.subject)
        halves = priced[item.subject]
        plan = existing or freeze_plan(item, definitions.get(workflow or "", ()), workflow, halves.without_runs, now)
        verdict = decide_arms(plan, TwoArms(halves.without_runs, halves.with_runs, matched=True), now,
                              inputs=halves.inputs())
        evaluations.append(Evaluation(item.subject, trial_owner(db, item), plan, verdict, existing is None))
    return evaluations


def _priced(db: sqlite3.Connection, now: datetime, arms: Mapping[str, Arms]) -> dict[str, Arms]:
    """The arms with each ended task's cost, through the same pricing the workflow verdicts use."""
    histories = tuple(WorkflowHistory(subject, None, (*half.with_runs, *half.without_runs))
                      for subject, half in arms.items())
    snapshot = price_snapshot(db, Snapshot(now.isoformat(), histories))
    priced = {}
    for history in snapshot.workflows:
        half = arms[history.ref]
        by_id = {run.run_id: run for run in history.runs}
        priced[history.ref] = replace(
            half, with_runs=tuple(by_id[run.run_id] for run in half.with_runs),
            without_runs=tuple(by_id[run.run_id] for run in half.without_runs))
    return priced
