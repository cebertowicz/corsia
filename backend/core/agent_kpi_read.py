"""Read synced KPI declarations and their scoped outcome facts."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from core.agent_kpi_operands import Event, timestamp
from core.measurement_scope import MeasurementScope, scope_cte
from infra.measure_registry import workflow_for_file
from tools.workflow_owners import thread_workflow_owners


def definitions(db: sqlite3.Connection, owner: str) -> list[dict[str, Any]]:
    """Return latest live definitions and file metadata for an agent."""
    rows = db.execute("""SELECT d.kind,d.name,d.version,d.spec_json,d.file_path,d.version_attr
        FROM measure_definitions d
        WHERE d.owner_thread_id=? AND d.version=(SELECT MAX(v.version) FROM measure_definitions v
          WHERE v.owner_thread_id=d.owner_thread_id AND v.kind=d.kind AND v.name=d.name)
          AND d.removed=0""", (owner,)).fetchall()
    workflows = thread_workflow_owners(owner, db)
    return [{"kind": row[0], "name": row[1], "version": row[2], "spec": json.loads(row[3]),
             "file_path": row[4], "version_attr": row[5],
             "workflow": workflow_for_file(row[4], owner, workflows)} for row in rows]


def event_names(spec: Mapping[str, Any]) -> set[str]:
    """Find each event needed by a KPI, including match and coverage operands."""
    result: set[str] = set()
    operands = [spec.get("num"), spec.get("den")]
    coverage = spec.get("coverage") or {}
    operands.extend([coverage.get("of"), coverage.get("labelled")])
    for operand in operands:
        if not isinstance(operand, dict):
            continue
        for entry in [operand, *operand.get("match", [])]:
            name = entry.get("event")
            if isinstance(name, str):
                result.add(name)
            elif isinstance(name, list):
                result.update(name)
    return result


def _label(row: sqlite3.Row, owner: str, workers: set[str]) -> str:
    if row["provenance"] in ("observed", "instrumented"):
        return "Observed"
    if row["provenance"] == "imported":
        return "Imported"
    if row["corroborated_by"]:
        return "Corroborated"
    emitter = row["agent_thread_id"]
    return "Self-reported" if emitter == owner or emitter in workers else "Reported by another agent"


def facts(db: sqlite3.Connection, owner: str, project: str | None, declarations: list[dict[str, Any]],
          names: set[str], repositories: set[str]) -> list[Event]:
    """Load owner/worker facts and platform facts in the project's repository scope."""
    user_id = db.execute("SELECT user_id FROM threads WHERE id=?", (owner,)).fetchone()[0]
    params = MeasurementScope(project="all", agent=owner).params(user_id)
    # The scope closure roots only at an active agent; the owner's own events count for a plain thread too.
    workers = {r[0] for r in db.execute(scope_cte() + "SELECT id FROM scope_threads", params)} | {owner}
    declared = {d["name"]: d for d in declarations if d["kind"] == "event"}
    user_threads = {r[0] for r in db.execute("SELECT id FROM threads WHERE user_id=?", (user_id,))}
    rows = db.execute("""SELECT event_name,ts,subject_key,project_id,agent_thread_id,attributes,provenance,corroborated_by
        FROM outcome_events WHERE event_name IN (SELECT value FROM json_each(?)) AND retracted_at IS NULL""",
        (json.dumps(sorted(names | set(declared))),)).fetchall()
    result = []
    for row in rows:
        attrs = json.loads(row["attributes"])
        definition = declared.get(row["event_name"])
        if definition is not None:
            if row["agent_thread_id"] not in workers or row["project_id"] != project:
                continue
        elif row["event_name"].startswith("github."):
            if str(attrs.get("repo", "")).lower() not in repositories:
                continue
        elif project is None or row["project_id"] != project or row["agent_thread_id"] not in user_threads:
            continue
        attrs["_declared_by_owner"] = definition is not None and row["agent_thread_id"] in workers
        version = attrs.get("workflow_version")
        if not version and definition and definition["version_attr"]:
            version = attrs.get(definition["version_attr"])
        result.append(Event(row["event_name"], timestamp(row["ts"]), attrs, row["subject_key"], version,
                            _label(row, owner, workers)))
    return result
