"""Declared Agent KPI measures and the common outcome-event admission path."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from infra.database import write_transaction
from infra.outcome_event_attributes import AttributeSpec
from infra.outcome_events import RecordResult, record_outcome_event

_NAMESPACE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_RESERVED = {"corsia", "github", "measurement", "test", "script", "agent", "otel"}
_KINDS = {"id", "login", "count", "num"}


@dataclass(frozen=True)
class Admission:
    result: RecordResult | None
    refusal: str | None = None


class MeasureRegistry:
    """The in-process declared-event registry; it swaps after a committed sync."""

    def __init__(self) -> None:
        self.events: dict[str, tuple[str, dict[str, AttributeSpec], tuple[str, ...]]] = {}
        self.namespaces: dict[str, str] = {}
        self.workflows: dict[str, str] = {}

    def rebuild(self, db: sqlite3.Connection) -> None:
        self.events.clear()
        self.namespaces = dict(db.execute("SELECT namespace, owner_thread_id FROM measure_namespaces"))
        rows = db.execute(
            "SELECT owner_thread_id,name,spec_json FROM measure_definitions "
            "WHERE kind='event' AND (owner_thread_id,name,version) IN "
            "(SELECT owner_thread_id,name,max(version) FROM measure_definitions "
            "WHERE kind='event' GROUP BY owner_thread_id,name)"
        )
        for owner, name, raw in rows:
            spec = json.loads(raw)
            self.events[name] = (owner, _attribute_specs(spec["attributes"]), tuple(spec["dedupe"]))


REGISTRY = MeasureRegistry()


def _attribute_specs(raw: dict[str, Any]) -> dict[str, AttributeSpec]:
    result: dict[str, AttributeSpec] = {}
    for name, kind in raw.items():
        if isinstance(kind, str):
            result[name] = AttributeSpec(kind)
        else:
            result[name] = AttributeSpec("enum", frozenset(kind["enum"]))
    return result


def declared_attribute_specs(event_name: str) -> dict[str, AttributeSpec] | None:
    entry = REGISTRY.events.get(event_name)
    return entry[1] if entry else None


def _line_error(path: str, message: str) -> ValueError:
    return ValueError(f"measures.yaml {path} {message}")


def _workflow_owner(path: str, workflow: str) -> str:
    """Read the workflow owner from the declaration's sibling SKILL.md."""
    skill = Path(path).parent / "SKILL.md"
    try:
        text = skill.read_text(encoding="utf-8")
        frontmatter = yaml.safe_load(text.split("---", 2)[1])
        owner = frontmatter["corsia"]["workflow"]["owner"]
    except (IndexError, KeyError, OSError, TypeError, yaml.YAMLError):
        raise ValueError(f"measure_sync: workflow {workflow} has no owner, so its measures.yaml can't be synced. Add owner: <your thread id> under corsia: then workflow: in its SKILL.md frontmatter, or keep your measures in your own .threads folder.") from None
    if not isinstance(owner, str) or not owner:
        raise ValueError(f"measure_sync: workflow {workflow} has no owner, so its measures.yaml can't be synced. Add owner: <your thread id> under corsia: then workflow: in its SKILL.md frontmatter, or keep your measures in your own .threads folder.")
    return owner


def _validate(data: dict[str, Any], owner: str) -> tuple[str, list[tuple[str, str, dict[str, Any]]]]:
    if data.get("schema") != "corsia.measures/1":
        raise _line_error("schema", 'must be "corsia.measures/1"; use the required schema')
    namespace = data.get("namespace")
    if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace) or namespace in _RESERVED:
        raise _line_error("namespace", "must be a non-reserved lowercase namespace")
    items: list[tuple[str, str, dict[str, Any]]] = []
    events = data.get("events", {})
    if not isinstance(events, dict) or not events:
        raise _line_error("events", "must declare at least one event")
    if len(events) > 64:
        raise _line_error("events", "allows at most 64 events")
    for name, spec in events.items():
        if not isinstance(name, str) or not name.startswith(namespace + "."):
            raise _line_error("events", "may declare only this namespace")
        if not isinstance(spec, dict) or set(spec) - {"attributes", "dedupe", "corroborate", "keep_days"}:
            raise _line_error(f"events.{name}", "has an unknown field")
        attrs = spec.get("attributes")
        dedupe = spec.get("dedupe")
        if not isinstance(attrs, dict) or not isinstance(dedupe, list) or not dedupe:
            raise _line_error(f"events.{name}", "needs attributes and dedupe")
        if len(attrs) > 16:
            raise _line_error(f"events.{name}.attributes", "allows at most 16 attributes")
        for key, kind in attrs.items():
            valid = isinstance(kind, str) and kind in _KINDS
            valid = valid or (isinstance(kind, dict) and set(kind) == {"enum"} and isinstance(kind["enum"], list) and 0 < len(kind["enum"]) <= 20 and all(isinstance(x, str) for x in kind["enum"]))
            if not valid:
                raise _line_error(f"events.{name}.attributes.{key}", "kind must be id, login, count, num or enum")
        if any(key != "run_id" and key not in attrs for key in dedupe):
            raise _line_error(f"events.{name}.dedupe", "names an undeclared attribute")
        items.append(("event", name, spec))
    for kind, key in (("kpi", "kpis"), ("view", "views")):
        values = data.get(key, {} if kind == "kpi" else [])
        iterable = values.items() if isinstance(values, dict) else ((item.get("id"), item) for item in values if isinstance(item, dict))
        for name, spec in iterable:
            if not name or not isinstance(spec, dict):
                raise _line_error(key, "contains an invalid declaration")
            if kind == "kpi":
                deciding = spec.get("can_decide", False)
                if spec.get("decides_as") and not deciding:
                    raise _line_error(f"kpi {name} decides_as", "needs can_decide: true. Add can_decide: true, or remove decides_as.")
                if spec.get("decides_as") not in (None, "quality", "spend"):
                    raise _line_error(f"kpi {name} decides_as", "must be quality or spend")
                if deciding and ("target" not in spec or "review" not in spec):
                    raise _line_error(f"kpi {name} can_decide", "needs target and review, or set can_decide: false.")
                if deciding and not (spec.get("coverage") or any(item[2].get("corroborate") for item in items if item[0] == "event")):
                    raise _line_error(f"kpi {name} can_decide", "needs a corroborated event or coverage, or set can_decide: false.")
            items.append((kind, str(name), spec))
    if data.get("version_attr") and not data.get("workflow"):
        raise _line_error("version_attr", "is allowed only in a file that names a workflow you own. Add workflow: <name>, or remove version_attr.")
    return namespace, items


def sync(db: sqlite3.Connection, *, owner_thread_id: str, path: str, text: str) -> dict[str, Any]:
    """Validate and atomically persist a declaration file, then refresh the registry."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("measures.yaml must be a mapping")
    namespace, items = _validate(data, owner_thread_id)
    workflow = data.get("workflow")
    if workflow is not None and not isinstance(workflow, str):
        raise _line_error("workflow", "must name an owned workflow")
    if workflow and _workflow_owner(path, workflow) != owner_thread_id:
        raise ValueError(f"measure_sync: workflow {workflow} is owned by another agent; only its owner can sync it.")
    sha = hashlib.sha256(text.encode()).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    changes: list[dict[str, str]] = []
    with write_transaction(db, scope="measure_sync") as connection:
        existing = connection.execute("SELECT owner_thread_id FROM measure_namespaces WHERE namespace=?", (namespace,)).fetchone()
        if existing and existing[0] != owner_thread_id:
            raise ValueError(f'Namespace "{namespace}" belongs to another agent (thread {existing[0]}); you can emit your own namespace only. Use your own namespace, or pick a new one in measures.yaml.')
        connection.execute("INSERT OR IGNORE INTO measure_namespaces(namespace,owner_thread_id) VALUES (?,?)", (namespace, owner_thread_id))
        for kind, name, spec in items:
            normalized = json.dumps(spec, sort_keys=True, separators=(",", ":"))
            row = connection.execute("SELECT version,spec_json FROM measure_definitions WHERE owner_thread_id=? AND kind=? AND name=? ORDER BY version DESC LIMIT 1", (owner_thread_id, kind, name)).fetchone()
            if row and row[1] == normalized:
                changes.append({"name": name, "status": "unchanged"})
                continue
            version = 1 if row is None else row[0] + 1
            connection.execute("INSERT INTO measure_definitions VALUES (?,?,?,?,?,?,?,?)", (owner_thread_id, kind, name, version, normalized, path, sha, now))
            changes.append({"name": name, "status": "added" if version == 1 else f"changed v{version - 1}→v{version}"})
    REGISTRY.rebuild(db)
    if workflow:
        REGISTRY.workflows[workflow] = owner_thread_id
    return {"namespace": namespace, "items": changes}


def admit(db: sqlite3.Connection, *, owner_thread_id: str, event_name: str, attributes: dict[str, Any], source: str = "agent", project_id: str | None = None, run_id: str | None = None, workflow_ref: str | None = None, ts: str | None = None) -> Admission:
    """Use the same declared-spec admission from scripts and OTLP fixtures."""
    entry = REGISTRY.events.get(event_name)
    day = datetime.now(timezone.utc).date().isoformat()
    namespace = event_name.split(".", 1)[0]
    if entry is None:
        _count(db, "name_undeclared", event_name, owner_thread_id, day)
        return Admission(None, f'"{event_name}" is not declared in your measures.yaml; the record was dropped. Declare it and run measure_sync.')
    if entry[0] != owner_thread_id:
        _count(db, "namespace_not_owned", event_name, owner_thread_id, day)
        return Admission(None, f'Namespace "{namespace}" belongs to another agent; the record was dropped. Use your own namespace.')
    if workflow_ref and REGISTRY.workflows.get(workflow_ref) != owner_thread_id:
        _count(db, "identity_mismatch", event_name, owner_thread_id, day)
        return Admission(None, f'corsia.workflow.ref "{workflow_ref}" is owned by another agent; the record was dropped. Use a workflow you own, or leave the ref out.')
    if run_id:
        attributes = {**attributes, "run_id": run_id}
    dedupe = "|".join(str(attributes.get(key, "")) for key in entry[2])
    result = record_outcome_event(db, dedupe_key=f"{event_name}:{dedupe}", event_name=event_name, source=source, provenance="agent_recorded", ts=ts or datetime.now(timezone.utc).isoformat(), project_id=project_id, agent_thread_id=owner_thread_id, attribution="caller", attributes=attributes)
    return Admission(result)


def _count(db: sqlite3.Connection, counter: str, name: str, owner: str, day: str) -> None:
    db.execute("INSERT INTO outcome_event_counters(counter,event_name,agent_thread_id,day,n) VALUES (?,?,?,?,1) ON CONFLICT(counter,event_name,agent_thread_id,day) DO UPDATE SET n=n+1", (counter, name, owner, day))
