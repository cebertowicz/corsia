"""The Workflows section: the page's coverage and the workflow runs in its window.

Every field is read from the module that owns the fact, so a correction to one rule lands here by itself:

| Field | Owner |
|---|---|
| which nodes are runs | ``core.workflow_builtin_operands.scoped_runs`` and ``core.workflow_verdict_read.run_roots`` |
| the run, its version, state, corrections, cost and model group | ``core.workflow_verdict_read.read_snapshot`` and ``price_snapshot`` (``Measurement``'s own price chain) |
| whether a task ran under a workflow | ``api.measurement_workflow_share.under_workflow``, the rule the ``under_workflow_pct`` series counts by too |
| what a task is, and the window it is counted in | ``api.measurement_queries.outcome_ctes`` with the page grain |
| how a task was created (coverage by origin) | the ``provenance`` its plan writer stamped on its ``node_created`` event, and ``plan_nodes.follow_up_of`` |
| a run's end, its steps and their rounds | ``plans.run_end.records`` (the ``workflow_run_ended`` record) |
| working time | ``plans.node_cost_scoped.working_seconds``, the plan-node cost rollup's own rolled ``execution.seconds`` |
| a step's title, an agent's label | the plan node's ``title``, the thread's ``title`` (as ``/agents`` labels it) |
| the runs never compared, and where the comparison stands | ``core.workflow_verdict_read.read_snapshot`` (``WorkflowHistory.excluded``) and ``workflow_verdict_rules.compute``; a reached verdict from its own record via ``workflow_verdict_evaluator.reached_detail`` |

A fact with no source on this install reads ``null``, never 0: a run that has not ended has no step,
gate or cost figure; a workflow's human name is not recorded anywhere (a skill's frontmatter carries only
``name`` and ``description``, and a run phase's title is that run's own prose); the owner's own measure has
no per-run reader until the KPI formula engine supplies one (``core.workflow_verdict_read.KpiReader``).

Only judged runs are listed: ``read_snapshot`` keeps unversioned and locally-edited runs in ``excluded``
because no version comparison can use them, and the list is the same set the verdicts count.

The run list is windowed; the excluded count and the verdict are not. They are read from the workflow's full
history, because a run outside the window is still a run the engine has or has not compared, and the next
look belongs to the whole version, not to the week being viewed.

They ignore the scope for the same reason: the engine pools a workflow version's runs by workflow reference
across the install, and the nightly job settles that one call. A scoped page that recomputed it from the
scope's runs would show a second, different call for the same version. The runs, steps, agents and measures
are the scope's.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query

from api.measurement_queries import outcome_ctes
from api.measurement_workflow_copy import summary as _summary
from api.measurement_workflow_threads import agents as _agents, labels as _labels, plan_targets as _plan_targets
from api.measurement_scope import resolve_grain, validate_scope
from core.corrections_recording import (
    SOURCE_KIND_CHAT_COMMENT, SOURCE_KIND_DECISION_ANSWER, SOURCE_KIND_DOC_COMMENT,
    SOURCE_KIND_MESSAGE, SOURCE_KIND_SENT_BACK,
)
from core.measurement_scope import Grain, MeasurementScope, Window, parse_zone, query_params, scope_cte
from core.workflow_builtin_operands import scoped_runs
from core.workflow_verdict_rules import compute
from core.workflow_verdict_types import Snapshot, WorkflowHistory, subject_of
from core.workflow_verdicts import Run, parse_ts
from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_bulk
from infra.packed_rows import fetch_all
from plans.public import node_working_seconds, run_end_records
from tools.workflow_task_fields import version_short_hash
from api.measurement_workflow_history import measures as _measures, version_summaries as _version_summaries, weekly as _weekly
from api.measurement_workflow_share import under_workflow

router = APIRouter()

#: How a run's failure came to light, as the section words it.
CAUGHT_AGENT = "agent_closed"
CAUGHT_SENT_BACK = "sent_back"
CAUGHT_LATER = "corrected_later"
#: What ``plans.run_end`` records for a step that was done, and for one the agent itself failed.
STEP_DONE = "done"
STEP_FAILED = "failed"
RUN_FAILED = "failed"
#: Person-sourced corrections: a later correction, as opposed to a reviewer sending a step back.
_PERSON_SOURCES = frozenset({SOURCE_KIND_MESSAGE, SOURCE_KIND_DOC_COMMENT, SOURCE_KIND_CHAT_COMMENT,
                             SOURCE_KIND_DECISION_ANSWER})


@router.get("/workflows")
async def measurement_workflows(
    project: str = Query(...),
    agent: str = Query(...),
    timezone_name: str = Query(..., alias="timezone"),
    grain: str = "days",
    coverage_only: bool = False,
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    """Return the Workflows section: coverage, then one entry per workflow with the runs in the window.
    ``coverage_only`` drops ``workflows`` and its history read, for a bounded agent read of coverage alone.

    The scope and the window are the page's own: ``validate_scope``'s composite scope and the grain's
    latest comparison window (7 completed days or 4 completed weeks) that ``/key-figures`` and ``/agents``
    answer for, in the viewer's ``timezone``. A run is listed under its workflow reference with the date it
    started in that zone, its version, the agent that ran it, its cost and working minutes, the required
    step that failed with how it was caught, its gate rounds and its owner's own measure; each is null
    where the install records none (see this module's table).
    """
    return await workflows_read(project, agent, resolve_grain(grain, timezone_name), user, coverage_only=coverage_only)


async def workflows_read(project: str, agent: str, page_grain: Grain, user: dict, *, coverage_only=False) -> dict[str, Any]:
    """The section for one resolved grain, on the bulk lane the priced chain needs."""

    def query() -> dict[str, Any]:
        db = get_db()
        try:
            selected_scope = validate_scope(db, user["id"], project, agent)
            return {"definition_version": 1, "scope": selected_scope.response(),
                    "period": page_grain.describe(),
                    **workflow_runs(db, user["id"], selected_scope, page_grain, coverage_only=coverage_only)}
        finally:
            db.close()

    return await run_bulk(query)


def workflow_runs(db: sqlite3.Connection, user_id: str, scope: MeasurementScope,
                  grain: Grain, *, now: datetime | None = None, coverage_only: bool = False) -> dict[str, Any]:
    """Coverage and the window's runs, for one already validated scope and grain; coverage alone if asked."""
    from core.workflow_verdict_read import read_snapshot

    window = grain.latest
    nodes, roots = scoped_runs(db, user_id, scope)
    parents = {str(row["id"]): row["parent_id"] for row in nodes}
    root_ids = frozenset(str(row["id"]) for row in roots)
    coverage = _coverage(db, scope.params(user_id), grain, window, parents, root_ids)
    if coverage_only:
        return {"window": {"from": window.first, "to": grain.period_end(window.last)}, "coverage": coverage}
    if not root_ids:
        return {"window": {"from": window.first, "to": grain.period_end(window.last)},
                "coverage": coverage, "workflows": []}
    in_window = _started_in_window(db, root_ids, grain, window)
    full = read_snapshot(db, now=now or datetime.now(timezone.utc),
                         workflow_refs=sorted({str(row["workflow_ref"]) for row in roots}))
    scoped = _priced_scope_runs(db, full, root_ids)
    windowed = replace(scoped, workflows=tuple(
        replace(history, runs=tuple(run for run in history.runs if run.run_id in in_window))
        for history in scoped.workflows))
    return {"window": {"from": window.first, "to": grain.period_end(window.last)},
            "coverage": coverage,
            "workflows": _entries(db, windowed, full, grain, scoped)}


def _priced_scope_runs(db: sqlite3.Connection, full: Snapshot, keep: frozenset[str]) -> Snapshot:
    """The scope's runs of this snapshot, from its first run to its latest, priced.

    ``read_snapshot`` reads every run of the named workflows on this install, so the scope's run ids decide
    what is kept; the weekly trends and version summaries need every one of them, not only the window's.
    """
    from core.workflow_verdict_read import price_snapshot

    narrowed = replace(full, workflows=tuple(
        replace(history, runs=tuple(run for run in history.runs if run.run_id in keep))
        for history in full.workflows), plans={}, reached=frozenset())
    return price_snapshot(db, narrowed) if keep else narrowed


def _coverage(db: sqlite3.Connection, params: dict[str, Any], grain: Grain, window: Window,
              parents: Mapping[str, str | None], root_ids: frozenset[str]) -> dict[str, Any]:
    """Tasks in the window that ran under a workflow, beside every task in the window, and by origin.

    The task set is Measurement's own accepted-task read, so this headline counts the same tasks the
    page's other panels count; membership is ``under_workflow``, the rule the series counts by.

    ``by_origin`` splits the same tasks by how each was created: the ``provenance`` its plan writer
    stamped on the task's ``node_created`` event (``null`` where none is recorded) and whether it is a
    follow-up of another node. The rows sum to the headline exactly, so the split never disagrees with it.
    """
    rows = fetch_all(db, scope_cte() + outcome_ctes(grain) + """
        SELECT o.id AS id, n.follow_up_of IS NOT NULL AS follow_up,
          (SELECT e.provenance FROM plan_node_events e WHERE e.node_id=o.id AND e.event='node_created'
           ORDER BY e.created_at LIMIT 1) AS provenance
        FROM outcome_rows o JOIN plan_nodes n ON n.id=o.id
        WHERE o.period IN (SELECT value FROM json_each(:periods))
    """, {**query_params({**params, "grain": grain}), "periods": json.dumps(sorted(window.periods))})
    origins: dict[tuple[str | None, bool], list[int]] = {}
    for row in rows:
        tally = origins.setdefault((row["provenance"], bool(row["follow_up"])), [0, 0])
        tally[0] += 1
        tally[1] += under_workflow(str(row["id"]), parents, root_ids)
    # Most tasks outside a workflow first: what holds the share down reads first.
    ordered = sorted(origins.items(), key=lambda item: (item[1][1] - item[1][0], item[0][0] or "", item[0][1]))
    return {"under_workflow": sum(under for _, (_, under) in ordered), "all_tasks": len(rows),
            "by_origin": [{"provenance": provenance, "follow_up": follow_up, "all_tasks": total,
                           "under_workflow": under}
                          for (provenance, follow_up), (total, under) in ordered]}


def _started_in_window(db: sqlite3.Connection, run_ids: frozenset[str], grain: Grain,
                       window: Window) -> frozenset[str]:
    """Which of these runs started inside the window, by the grain's own window test."""
    if not run_ids:
        return frozenset()
    rows = fetch_all(
        db, f"SELECT id FROM plan_nodes WHERE id IN (SELECT value FROM json_each(:ids))"
        f" AND {grain.window_sql('created_at', window)}",
        {"ids": json.dumps(sorted(run_ids))})
    return frozenset(str(row["id"]) for row in rows)


def _entries(db: sqlite3.Connection, windowed: Snapshot, full: Snapshot, grain: Grain,
             scoped: Snapshot) -> list[dict[str, Any]]:
    """One entry per workflow, biggest first, each with its runs in start order.

    ``windowed`` carries the runs to list, ``full`` the history the excluded count and the verdict are read
    from: a run outside the window is not listed, but it is still a run the engine has or has not compared.
    """
    history_runs = [run for history in scoped.workflows for run in history.runs]
    if not history_runs:
        return []
    ids = [run.run_id for run in history_runs]
    records = run_end_records(db, ids)
    titles = _step_titles(db, {node_id for record in records.values()
                               for step in record["steps"] for node_id in step["node_ids"]})
    agents = _agents(db, ids)
    targets = _plan_targets(db, ids)
    owners = {history.owner for history in full.workflows if history.owner}
    labels = _labels(db, {agent["id"] for agent in agents.values() if agent} | owners)
    working = _working_seconds(db, ids)
    verdicts, calls = _verdicts(db, full)
    by_ref = {history.ref: history for history in full.workflows}
    rows = {run.run_id: _run_row(run, records.get(run.run_id), titles, agents.get(run.run_id),
                                 working.get(run.run_id), parse_zone(grain.zone))
            for run in history_runs}
    for run_id, row in rows.items():
        row["plan_target"] = targets.get(run_id)
    history_by_ref = {history.ref: history for history in scoped.workflows}
    entries = []
    for history in windowed.workflows:
        all_runs = history_by_ref[history.ref].runs
        if not all_runs:
            continue
        entry = _entry(history, rows, labels, by_ref[history.ref], verdicts.get(history.ref))
        entry["history"] = [rows[run.run_id] for run in sorted(all_runs, key=lambda run: (parse_ts(run.started_at), run.run_id))]
        entry["weekly"] = _weekly(entry["history"], resolve_grain("weeks", grain.zone))
        entry["measures"] = _measures(entry["runs"])
        entry["versions"] = _version_summaries(entry["history"], by_ref[history.ref], parse_zone(grain.zone))
        versions = by_ref[history.ref].versions()
        current = versions[-1] if versions else None
        entry["test_start"] = (parse_ts(by_ref[history.ref].first_use(current)).astimezone(parse_zone(grain.zone)).date().isoformat()
                               if current and len(versions) > 1 else None)
        entry["summary"] = _summary(entry["verdict"], entry["excluded"], entry["owner"] is not None,
                                    calls.get(history.ref, []))
        entry["agent_measures"] = {agent_id: _measures([row for row in entry["runs"] if (row["agent"] or {}).get("id", "") == agent_id])
                                   for agent_id in {(row["agent"] or {}).get("id", "") for row in entry["runs"]}}
        entry["steps"] = sorted({titles[node_id] for run in all_runs if (record := records.get(run.run_id))
                                 for step in record["steps"] for node_id in step["node_ids"] if node_id in titles})
        entries.append(entry)
    return sorted(entries, key=lambda entry: (-len(entry["runs"]), entry["ref"]))


def _entry(history: WorkflowHistory, rows: Mapping[str, dict[str, Any]], labels: Mapping[str, str | None],
           full_history: WorkflowHistory, verdict: dict[str, Any] | None) -> dict[str, Any]:
    """One workflow: its reference, its owner, its runs from first started to last, and its comparison state.

    ``excluded`` is the engine's own count of runs it never judged, and ``verdict`` where the current
    version's comparison stands; both come from ``full_history``, the workflow's whole history.
    """
    return {
        "ref": history.ref,
        # No source on dev for a human name: see this module's note; the reference is the identity.
        "name": None,
        "owner": None if history.owner is None else {"id": history.owner, "label": labels.get(history.owner)},
        "runs": [rows[run.run_id] for run in sorted(history.runs, key=lambda run: (parse_ts(run.started_at), run.run_id))],
        "excluded": dict(full_history.excluded),
        "verdict": verdict,
    }


def _verdicts(db: sqlite3.Connection, full: Snapshot) -> tuple[dict[str, dict[str, Any] | None], dict[str, list[dict[str, Any]]]]:
    """Where each workflow's current version stands with the engine, over its full history.

    ``workflow_verdict_rules.compute`` is the evaluator's own assembly — the recorded plan where a version
    has one, a frozen plan otherwise — so the floors, the next look and the reason words are the engine's,
    never recomputed here. A version whose verdict was reached is read from its record instead, because the
    engine never recomputes one. Null only where no run of the workflow carries an exact version: there is
    nothing to compare, and no zero is standing in for it.
    """
    from core.workflow_verdict_evaluator import reached_detail

    current = {history.ref: (versions[-1] if (versions := history.versions()) else None)
               for history in full.workflows}
    subjects = {subject_of("workflow", ref, version) for ref, version in current.items() if version}
    stored = reached_detail(db, sorted(subjects & full.reached))
    open_versions = {evaluation.subject: evaluation.verdict for evaluation in compute(full)}
    verdicts: dict[str, dict[str, Any] | None] = {}
    calls: dict[str, list[dict[str, Any]]] = {}
    for ref, version in current.items():
        subject = subject_of("workflow", ref, version) if version else ""
        if subject in stored:
            record = stored[subject]
            verdicts[ref] = {"word": record["word"], "reason": record["reason"],
                             "look": record.get("look"), "inputs": dict(record.get("inputs") or {})}
            calls[ref] = [{"name": call[0], "call": call[1], "reason": call[2]}
                          for call in record.get("calls", []) if len(call) >= 3]
        elif subject in open_versions:
            verdict = open_versions[subject]
            verdicts[ref] = {"word": verdict.word, "reason": verdict.reason, "look": verdict.look,
                             "inputs": dict(verdict.inputs)}
            calls[ref] = [{"name": call.name, "call": call.call, "reason": call.reason} for call in verdict.calls]
        else:
            # No run of this workflow carries an exact version, so there is no version to compare.
            verdicts[ref] = None
    return verdicts, calls


def _run_row(run: Run, record: Mapping[str, Any] | None, titles: Mapping[str, str],
             agent: dict[str, Any] | None, working: float | None, zone: ZoneInfo) -> dict[str, Any]:
    """One run as the section reads it, with null for every figure this install does not record."""
    return {
        "run_id": run.run_id,  # the run's root plan node, which is what the plan surface opens
        "started": parse_ts(run.started_at).astimezone(zone).date().isoformat(),
        "version": version_short_hash(run.version),
        "version_id": run.version,
        "agent": agent,
        "cost": run.cost,
        "minutes": None if working is None else round(working / 60, 1),
        "failed_step": _failed_step(record, titles),
        "focus_node_id": _failed_step_id(record) or run.run_id,
        "caught": _caught(run, record),
        "gate_rounds": _gate_rounds(record),
        "corrections": None if run.terminal_at is None else len({
            (fact.source_kind, fact.source_id) for fact in run.corrections if fact.is_correction and
            fact.source_kind in _PERSON_SOURCES | {SOURCE_KIND_SENT_BACK}}),
        "finished": record is not None,
        "first_try": None if record is None else all(
            step["state"] == STEP_DONE and int(step["rounds"]) == 0 and
            step.get("gate_answer") != "changes_requested"
            for step in record["steps"] if step["required"]),
        "owner_measure": None,  # No per-run owner measure exists yet: read_snapshot fills Run.kpi_rows only through a KpiReader, which nothing on dev passes.
    }


def _failed_step(record: Mapping[str, Any] | None, titles: Mapping[str, str]) -> str | None:
    """The title of the required step the run's record does not settle as done, or None.

    ``plans.run_end`` owns whether the run failed at all, and records its steps in definition order;
    this names the first required step it did not settle as done and reads that node's title.
    """
    node_id = _failed_step_id(record)
    return titles.get(node_id) if node_id else None


def _failed_step_id(record: Mapping[str, Any] | None) -> str | None:
    if record is None or record.get("outcome") != RUN_FAILED:
        return None
    for step in record["steps"]:
        if step["required"] and step["state"] != STEP_DONE and step["node_ids"]:
            return str(step["node_ids"][-1])
    return None


def _caught(run: Run, record: Mapping[str, Any] | None) -> str | None:
    """How the run came to light as wrong: the agent's own failed step, a send-back, or a later correction.

    The order is the order the facts can arrive: a failed required step ends the run
    (``plans.run_end.fail_run``), a person's send-back keeps it running, and a correction follows a run
    that settled. A run that settled clean and was never corrected reads None; a run whose only
    not-done step was closed some other way (the person's Won't do, a handed-off gate) reads None too,
    since none of the three words fits it.
    """
    if record is not None and any(step["required"] and step["state"] == STEP_FAILED for step in record["steps"]):
        return CAUGHT_AGENT
    if (any(fact.source_kind == SOURCE_KIND_SENT_BACK for fact in run.corrections) or
            record is not None and any(step.get("gate_answer") == "changes_requested" for step in record["steps"])):
        return CAUGHT_SENT_BACK
    if any(fact.is_correction and fact.source_kind in _PERSON_SOURCES for fact in run.corrections):
        return CAUGHT_LATER
    return None


def _gate_rounds(record: Mapping[str, Any] | None) -> int | None:
    """How many times the run went through its acceptance gate, or None when it has none.

    ``plans.run_end`` records each step's re-runs as ``rounds`` (0 for a step tried once) and names a
    step's gate mode only for an acceptance gate, so one gate step's rounds plus its first try is the run's.
    """
    if record is None:
        return None
    gates = [step for step in record["steps"] if step["gate_mode"] is not None]
    return sum(int(step["rounds"]) + 1 for step in gates) if gates else None


def _step_titles(db: sqlite3.Connection, node_ids: Iterable[str]) -> dict[str, str]:
    """The title of each step node the run records list."""
    ids = sorted(set(node_ids))
    if not ids:
        return {}
    return {str(row["id"]): str(row["title"]) for row in fetch_all(
        db, "SELECT id,title FROM plan_nodes WHERE id IN (SELECT value FROM json_each(:ids))",
        {"ids": json.dumps(ids)})}



def _working_seconds(db: sqlite3.Connection, run_ids: list[str]) -> dict[str, float]:
    """Each run's own working time, from the cost rollup that records it (subtree and carried plans)."""
    return {str(run_id): float(seconds) for run_id, seconds in node_working_seconds(run_ids, db=db).items()}
