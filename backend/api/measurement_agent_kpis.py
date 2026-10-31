"""Read an agent's declared KPI values from Measurement's event store."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.measurement_partial import grain_instant
from api.measurement_scope import MeasurementScope, resolve_grain, validate_scope
from core.agent_kpi_formula import evaluate, prepare
from core.agent_kpi_operands import Event, timestamp
from core.agent_kpi_read import definitions, event_names, facts
from core.correction_attribution import attributed_facts
from core.workflow_builtin_operands import Value, prepare_builtin, read_builtin, scoped_runs
from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_bulk
from tools.workflow_task_fields import version_short_hash

router = APIRouter()


def _repositories(db, project: str | None) -> set[str]:
    if project is None:
        return set()
    from events.connectors.github_notifications_feed import github_repositories_for_policy
    from infra.project_repo_policy import list_registered_repo_policies

    return {repo.lower() for policy in list_registered_repo_policies(db=db, project_id=project)
            for repo in github_repositories_for_policy(policy)}


def _versions(rows) -> list[dict[str, Any]]:
    first: dict[str, datetime] = {}
    for row in rows:
        version = row.version or ""
        if version not in first or row.ts < first[version]:
            first[version] = row.ts
    ordered = sorted((version for version in first if version not in ("", "legacy")), key=lambda version: first[version])
    labels: list[dict[str, Any]] = [{"version": version, "label": f"v{index}", "first_seen": first[version].isoformat(),
               "short_hash": version_short_hash(version)} for index, version in enumerate(ordered, 1)]
    if "legacy" in first:
        labels.insert(0, {"version": "legacy", "label": "Legacy (rebuilt)", "first_seen": first["legacy"].isoformat(), "short_hash": None})
    if "" in first:
        labels.append({"version": "", "label": "Unversioned", "first_seen": first[""].isoformat(), "short_hash": None})
    return labels


def _builtins(spec: dict[str, Any]) -> set[str]:
    needed = {op['builtin'] for op in (spec['num'], spec.get('den', {})) if 'builtin' in op}
    if spec['formula'] == 'rate_per_run':
        needed.add('runs')
    return needed


def _builtin_value(result, version: str | None) -> Value:
    if result.reason:
        return Value(None, 0, reason=result.reason)
    values = list(result.versions.values()) if version is None else [
        result.versions.get(version or None, Value(0, 0))]
    known = [entry.value for entry in values if entry.value is not None]
    n = sum(entry.n for entry in values)
    unknown = sum(entry.unknown for entry in values)
    amount = sum(known) if known else None
    if n and n == unknown and all(entry.value is None or entry.n == 0 for entry in values):
        amount = None
    return Value(amount, n, unknown)


def _read(user_id: str, project: str, agent: str, grain) -> dict[str, Any]:
    db = get_db()
    try:
        scope = validate_scope(db, user_id, project, agent)
        if scope.agent is None:
            raise HTTPException(400, "agent is required")
        actual_project = db.execute("SELECT project_id FROM threads WHERE id=?", (agent,)).fetchone()[0]
        return _read_scoped(db, user_id, scope, actual_project, grain)
    finally:
        db.close()


def read_own(user_id: str, thread_id: str, grain) -> dict[str, Any]:
    """The calling thread's own KPIs, promoted agent or not; the caller's identity is the authorization."""
    db = get_db()
    try:
        row = db.execute("SELECT project_id FROM threads WHERE id=? AND user_id=?", (thread_id, user_id)).fetchone()
        if not row:
            raise HTTPException(404, "thread not found")
        return _read_scoped(db, user_id, MeasurementScope(project=row[0], agent=thread_id), row[0], grain)
    finally:
        db.close()


def _read_scoped(db, user_id: str, scope: MeasurementScope, actual_project: str, grain) -> dict[str, Any]:
    agent = scope.agent
    declarations = definitions(db, agent)
    kpis = [item for item in declarations if item["kind"] == "kpi"]
    names = set().union(*(event_names(item["spec"]) for item in kpis))
    events = facts(db, agent, actual_project, declarations, names, _repositories(db, actual_project))
    boundaries = [grain_instant(start) for start in grain.starts] + [grain_instant(grain.current_end)]
    now = datetime.now(timezone.utc)
    workflow_names: dict[str, set[str]] = {}
    for item in kpis:
        if item['workflow'] and (needed := _builtins(item['spec'])):
            workflow_names.setdefault(item['workflow'], set()).update(needed)
    ledger = attributed_facts(db) if any('corrections' in wanted for wanted in workflow_names.values()) else []
    scoped = scoped_runs(db, user_id, scope) if workflow_names else None
    prepared = {workflow: prepare_builtin(db, workflow, user_id, scope, wanted, ledger, scoped)
                for workflow, wanted in workflow_names.items()}
    windows = list(zip(boundaries, boundaries[1:]))
    # Each workflow's builtins are read once per period, however many KPIs use them.
    builtin_reads = {workflow: [{name: read_builtin(db, builtin=name, workflow=workflow, user_id=user_id, scope=scope,
                                                    start=start.isoformat(), end=end.isoformat(),
                                                    prepared=prepared[workflow])
                                 for name in wanted} for start, end in windows]
                     for workflow, wanted in workflow_names.items()}
    results = []
    for item in kpis:
        spec = item["spec"]
        selected = prepare(spec, events)
        file_events = {entry["name"] for entry in declarations
                       if entry["kind"] == "event" and entry["file_path"] == item["file_path"]}
        builtin_names = _builtins(spec)
        version_rows = [event for event in events if event.name in file_events or event.name in event_names(spec)]
        if builtin_names and item['workflow']:
            version_rows += [Event('', timestamp(row['created_at']), {}, None, row['workflow_version'], 'Observed')
                             for row in prepared[item['workflow']].nodes]
        versions = _versions(version_rows)
        periods = []
        for index, period in enumerate((*grain.periods, grain.current)):
            start, end = windows[index]
            builtin_results = {name: builtin_reads[item['workflow']][index][name] for name in builtin_names}
            value = evaluate(spec, events, start, end,
                             builtins={name: _builtin_value(result, None) for name, result in builtin_results.items()},
                             selected=selected)
            value["partial"] |= now < end + timedelta(days=spec.get("settle_days", 0))
            by_version = []
            for version in versions:
                part = evaluate(spec, events, start, end, version["version"],
                                builtins={name: _builtin_value(result, version['version'])
                                          for name, result in builtin_results.items()}, selected=selected)
                part["partial"] |= now < end + timedelta(days=spec.get("settle_days", 0))
                by_version.append({"version": version["version"], **part})
            periods.append({"period": period, **value, "versions": by_version})
        results.append({"name": item["name"], "definition_version": item["version"], "spec": spec,
                        "workflow": item["workflow"], "versions": versions, "periods": periods})
    return {"scope": scope.response(), "kpis": results}


@router.get("/agent-kpis")
async def agent_kpis(project: str = Query(...), agent: str = Query(...),
                     timezone_name: str = Query(..., alias="timezone"), grain: str = "days",
                     user: dict = Depends(require_user)):
    """Return each synced KPI's per-period and per-version values."""
    page_grain = resolve_grain(grain, timezone_name)
    return await run_bulk(_read, user["id"], project, agent, page_grain)
