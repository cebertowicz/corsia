"""The Workflows section: the page's coverage and the workflow runs in its window.

Every field is read from the module that owns the fact, so a correction to one rule lands here by itself:

| Field | Owner |
|---|---|
| which nodes are runs | ``core.workflow_builtin_operands.scoped_runs`` and ``core.workflow_verdict_read.run_roots`` |
| the run, its version, state, corrections, cost and model group | ``core.workflow_verdict_read.read_snapshot`` and ``price_snapshot`` (``Measurement``'s own price chain) |
| which run a task belongs to | ``core.correction_attribution.workflow_run_of`` |
| what a task is, and the window it is counted in | ``api.measurement_queries.outcome_ctes`` with the page grain |
| a run's end, its steps and their rounds | ``plans.run_end.records`` (the ``workflow_run_ended`` record) |
| working time | ``plans.node_cost_scoped.project``, the plan-node cost rollup's own ``execution.seconds`` |
| a step's title, an agent's label | the plan node's ``title``, the thread's ``title`` (as ``/agents`` labels it) |

A fact with no source on this install reads ``null``, never 0: a run that has not ended has no step,
gate or cost figure; a workflow's human name is not recorded anywhere (a skill's frontmatter carries only
``name`` and ``description``, and a run phase's title is that run's own prose); the owner's own measure has
no per-run reader until the KPI formula engine supplies one (``core.workflow_verdict_read.KpiReader``).

Only judged runs are listed: ``read_snapshot`` keeps unversioned and locally-edited runs in ``excluded``
because no version comparison can use them, and the list is the same set the verdicts count.
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
from api.measurement_scope import resolve_grain, validate_scope
from core.correction_attribution import NOT_IN_WORKFLOW, UNATTRIBUTED, workflow_run_of
from core.corrections_recording import (
    SOURCE_KIND_CHAT_COMMENT, SOURCE_KIND_DECISION_ANSWER, SOURCE_KIND_DOC_COMMENT,
    SOURCE_KIND_MESSAGE, SOURCE_KIND_SENT_BACK,
)
from core.measurement_scope import Grain, MeasurementScope, Window, parse_zone, query_params, scope_cte
from core.workflow_builtin_operands import scoped_runs
from core.workflow_verdict_read import price_snapshot, read_snapshot
from core.workflow_verdict_types import Snapshot, WorkflowHistory
from core.workflow_verdicts import Run, parse_ts
from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_bulk
from plans import node_cost_scoped, run_end
from tools.workflow_task_fields import version_short_hash

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
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    """Return the Workflows section: coverage, then one entry per workflow with the runs in the window.

    The scope and the window are the page's own: ``validate_scope``'s composite scope and the grain's
    latest comparison window (7 completed days or 4 completed weeks) that ``/key-figures`` and ``/agents``
    answer for, in the viewer's ``timezone``. A run is listed under its workflow reference with the date it
    started in that zone, its version, the agent that ran it, its cost and working minutes, the required
    step that failed with how it was caught, its gate rounds and its owner's own measure; each is null
    where the install records none (see this module's table).
    """
    return await workflows_read(project, agent, resolve_grain(grain, timezone_name), user)


async def workflows_read(project: str, agent: str, page_grain: Grain, user: dict) -> dict[str, Any]:
    """The section for one resolved grain, on the bulk lane the priced chain needs."""

    def query() -> dict[str, Any]:
        db = get_db()
        try:
            selected_scope = validate_scope(db, user["id"], project, agent)
            return {"definition_version": 1, "scope": selected_scope.response(),
                    "period": page_grain.describe(),
                    **workflow_runs(db, user["id"], selected_scope, page_grain)}
        finally:
            db.close()

    return await run_bulk(query)


def workflow_runs(db: sqlite3.Connection, user_id: str, scope: MeasurementScope,
                  grain: Grain, *, now: datetime | None = None) -> dict[str, Any]:
    """Coverage and the window's runs, for one already validated scope and grain."""
    window = grain.latest
    nodes, roots = scoped_runs(db, user_id, scope)
    parents = {str(row["id"]): row["parent_id"] for row in nodes}
    root_ids = frozenset(str(row["id"]) for row in roots)
    in_window = _started_in_window(db, root_ids, grain, window)
    snapshot = _window_runs(db, {str(row["workflow_ref"]) for row in roots}, in_window, now=now)
    return {"window": {"from": window.first, "to": grain.period_end(window.last)},
            "coverage": _coverage(db, scope.params(user_id), grain, window, parents, root_ids),
            "workflows": _entries(db, snapshot, grain)}


def _window_runs(db: sqlite3.Connection, refs: Iterable[str], keep: frozenset[str],
                 *, now: datetime | None) -> Snapshot:
    """The judged runs of these workflows, narrowed to the scope's own runs in the window, then priced.

    ``read_snapshot`` reads every run of the named workflows on this install, so the scope's run ids and
    the window decide what is kept; pricing only what is kept holds the chain walk to the window.
    """
    snapshot = read_snapshot(db, now=now or datetime.now(timezone.utc), workflow_refs=sorted(refs))
    kept = tuple(replace(history, runs=tuple(run for run in history.runs if run.run_id in keep))
                 for history in snapshot.workflows)
    narrowed = replace(snapshot, workflows=kept, plans={}, reached=frozenset())
    return price_snapshot(db, narrowed) if keep else narrowed


def _coverage(db: sqlite3.Connection, params: dict[str, Any], grain: Grain, window: Window,
              parents: Mapping[str, str | None], root_ids: frozenset[str]) -> dict[str, int]:
    """Tasks in the window that ran under a workflow, beside every task in the window.

    The task set is Measurement's own accepted-task read, so this headline counts the same tasks the
    page's other panels count; membership in a run is ``workflow_run_of``'s rule, not a second one.
    """
    rows = db.execute(scope_cte() + outcome_ctes(grain) + """
        SELECT id FROM outcome_rows WHERE period IN (SELECT value FROM json_each(:periods))
    """, {**query_params({**params, "grain": grain}), "periods": json.dumps(sorted(window.periods))}).fetchall()
    tasks = [str(row["id"]) for row in rows]
    under = [task for task in tasks
             if workflow_run_of(task, parents, root_ids) not in (NOT_IN_WORKFLOW, UNATTRIBUTED)]
    return {"under_workflow": len(under), "all_tasks": len(tasks)}


def _started_in_window(db: sqlite3.Connection, run_ids: frozenset[str], grain: Grain,
                       window: Window) -> frozenset[str]:
    """Which of these runs started inside the window, by the grain's own window test."""
    if not run_ids:
        return frozenset()
    rows = db.execute(
        f"SELECT id FROM plan_nodes WHERE id IN (SELECT value FROM json_each(:ids))"
        f" AND {grain.window_sql('created_at', window)}",
        {"ids": json.dumps(sorted(run_ids))},
    ).fetchall()
    return frozenset(str(row["id"]) for row in rows)


def _entries(db: sqlite3.Connection, snapshot: Any, grain: Grain) -> list[dict[str, Any]]:
    """One entry per workflow, biggest first, each with its runs in start order."""
    runs = [run for history in snapshot.workflows for run in history.runs]
    if not runs:
        return []
    ids = [run.run_id for run in runs]
    records = run_end.records(db, ids)
    titles = _step_titles(db, {node_id for record in records.values()
                               for step in record["steps"] for node_id in step["node_ids"]})
    agents = _agents(db, ids)
    owners = {history.owner for history in snapshot.workflows if history.owner}
    labels = _labels(db, {agent["id"] for agent in agents.values() if agent} | owners)
    working = _working_seconds(db, ids)
    rows = {run.run_id: _run_row(run, records.get(run.run_id), titles, agents.get(run.run_id),
                                 working.get(run.run_id), parse_zone(grain.zone))
            for run in runs}
    entries = [_entry(history, rows, labels) for history in snapshot.workflows]
    return sorted(entries, key=lambda entry: (-len(entry["runs"]), entry["ref"]))


def _entry(history: WorkflowHistory, rows: Mapping[str, dict[str, Any]],
           labels: Mapping[str, str | None]) -> dict[str, Any]:
    """One workflow: its reference, its owner, and its runs from first started to last."""
    return {
        "ref": history.ref,
        # No source on dev for a human name: see this module's note; the reference is the identity.
        "name": None,
        "owner": None if history.owner is None else {"id": history.owner, "label": labels.get(history.owner)},
        "runs": [rows[run.run_id] for run in sorted(history.runs, key=lambda run: (parse_ts(run.started_at), run.run_id))],
    }


def _run_row(run: Run, record: Mapping[str, Any] | None, titles: Mapping[str, str],
             agent: dict[str, Any] | None, working: float | None, zone: ZoneInfo) -> dict[str, Any]:
    """One run as the section reads it, with null for every figure this install does not record."""
    return {
        "run_id": run.run_id,  # the run's root plan node, which is what the plan surface opens
        "started": parse_ts(run.started_at).astimezone(zone).date().isoformat(),
        "version": version_short_hash(run.version),
        "agent": agent,
        "cost": run.cost,
        "minutes": None if working is None else round(working / 60, 1),
        "failed_step": _failed_step(record, titles),
        "caught": _caught(run, record),
        "gate_rounds": _gate_rounds(record),
        "owner_measure": None,  # No per-run owner measure exists yet: read_snapshot fills Run.kpi_rows only through a KpiReader, which nothing on dev passes.
    }


def _failed_step(record: Mapping[str, Any] | None, titles: Mapping[str, str]) -> str | None:
    """The title of the required step the run's record does not settle as done, or None.

    ``plans.run_end`` owns whether the run failed at all, and records its steps in definition order;
    this names the first required step it did not settle as done and reads that node's title.
    """
    if record is None or record.get("outcome") != RUN_FAILED:
        return None
    for step in record["steps"]:
        if step["required"] and step["state"] != STEP_DONE and step["node_ids"]:
            return titles.get(str(step["node_ids"][-1]))
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
    if any(fact.source_kind == SOURCE_KIND_SENT_BACK for fact in run.corrections):
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
    return {str(row["id"]): str(row["title"]) for row in db.execute(
        "SELECT id,title FROM plan_nodes WHERE id IN (SELECT value FROM json_each(:ids))",
        {"ids": json.dumps(ids)},
    ).fetchall()}


def _agents(db: sqlite3.Connection, run_ids: Iterable[str]) -> dict[str, dict[str, Any] | None]:
    """The thread that ran each run: the owner of its plan, as ``/agents`` names a thread."""
    found: dict[str, dict[str, Any] | None] = {run_id: None for run_id in run_ids}
    rows = db.execute("""
        SELECT n.id AS run_id,p.owner_id AS id,t.title AS label FROM plan_nodes n
        JOIN plans p ON p.id=n.plan_id AND p.owner_kind='thread'
        LEFT JOIN threads t ON t.id=p.owner_id
        WHERE n.id IN (SELECT value FROM json_each(:ids))
    """, {"ids": json.dumps(sorted(run_ids))}).fetchall()
    found.update({str(row["run_id"]): {"id": str(row["id"]), "label": row["label"]} for row in rows})
    return found


def _labels(db: sqlite3.Connection, thread_ids: Iterable[str]) -> dict[str, str | None]:
    """Each thread's own title, which is what the page shows for an agent."""
    ids = sorted(thread_ids)
    if not ids:
        return {}
    return {str(row["id"]): row["title"] for row in db.execute(
        "SELECT id,title FROM threads WHERE id IN (SELECT value FROM json_each(:ids))",
        {"ids": json.dumps(ids)},
    ).fetchall()}


def _working_seconds(db: sqlite3.Connection, run_ids: list[str]) -> dict[str, float]:
    """Each run's own working time, from the cost rollup that records it (subtree and carried plans)."""
    _own, rolled, _initiatives, _ledger = node_cost_scoped.project([], run_ids, (), db=db)
    found = {}
    for run_id, figures in rolled.items():
        execution = figures.get("execution") or {}
        if execution.get("seconds") is not None:
            found[str(run_id)] = float(execution["seconds"])
    return found
