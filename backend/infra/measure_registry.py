"""Declared Agent KPI measures: the in-process registry and the ``measure_sync`` loader."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from infra.database import write_transaction
from infra.measure_validation import Lines, load, validate
from infra.outcome_event_attributes import AttributeSpec

REFUSAL_LIMIT = 20
# Reserved attributes the server stamps on every declared event; a file cannot declare them.
_STAMPED = {name: AttributeSpec("id") for name in ("run_id", "workflow_ref", "workflow_version")}
_INSERT = (
    "INSERT INTO measure_definitions (owner_thread_id,kind,name,version,spec_json,file_path,file_sha256,synced_at,removed) "
    "VALUES (?,?,?,?,?,?,?,?,?)"
)
_NO_OWNER = (
    "measure_sync: workflow {workflow} has no owner, so its measures.yaml can't be synced. "
    "Add `owner: <your thread id>` under `corsia:` then `workflow:` in its SKILL.md frontmatter, "
    "or keep your measures in your own .threads folder."
)


@dataclass(frozen=True)
class DeclaredEvent:
    owner: str
    specs: dict[str, AttributeSpec]
    dedupe: tuple[str, ...]
    declared: dict[str, Any]
    keep_days: int


class MeasureRegistry:
    """The declared-event registry; it swaps after a committed sync and loads lazily after boot."""

    def __init__(self) -> None:
        self.events: dict[str, DeclaredEvent] = {}
        self.namespaces: dict[str, str] = {}
        self.workflows: dict[str, str] = {}
        self.refusals: dict[str, deque[str]] = {}
        self.loaded = False

    def rebuild(self, db: sqlite3.Connection) -> None:
        self.events, self.workflows = {}, {}
        self.namespaces = dict(db.execute("SELECT namespace, owner_thread_id FROM measure_namespaces"))
        rows = db.execute(
            "SELECT owner_thread_id,name,spec_json FROM measure_definitions "
            "WHERE kind='event' AND removed=0 AND (owner_thread_id,name,version) IN "
            "(SELECT owner_thread_id,name,max(version) FROM measure_definitions "
            "WHERE kind='event' GROUP BY owner_thread_id,name)"
        )
        for owner, name, raw in rows:
            spec = json.loads(raw)
            specs = {**_attribute_specs(spec["attributes"]), **_STAMPED}
            self.events[name] = DeclaredEvent(owner, specs, tuple(spec["dedupe"]), spec["attributes"], int(spec.get("keep_days", 400)))
            if spec.get("_workflow"):
                self.workflows[spec["_workflow"]] = owner
        self.loaded = True

    def note(self, owner: str, text: str) -> None:
        """Keep the agent's last refusal texts in memory only; a restart loses them."""
        self.refusals.setdefault(owner, deque(maxlen=REFUSAL_LIMIT)).append(text)


REGISTRY = MeasureRegistry()


def _attribute_specs(raw: dict[str, Any]) -> dict[str, AttributeSpec]:
    result: dict[str, AttributeSpec] = {}
    for name, kind in raw.items():
        result[name] = AttributeSpec(kind) if isinstance(kind, str) else AttributeSpec("enum", frozenset(kind["enum"]))
    return result


def declared_attribute_specs(event_name: str) -> dict[str, AttributeSpec] | None:
    entry = REGISTRY.events.get(event_name)
    return entry.specs if entry else None


def _workflow_owner(path: str, workflow: str) -> str:
    """Read the workflow owner from the declaration's sibling SKILL.md."""
    skill = Path(path).parent / "SKILL.md"
    try:
        text = skill.read_text(encoding="utf-8")
        owner = yaml.safe_load(text.split("---", 2)[1])["corsia"]["workflow"]["owner"]
    except (IndexError, KeyError, OSError, TypeError, yaml.YAMLError):
        owner = None
    if not isinstance(owner, str) or not owner:
        raise ValueError(_NO_OWNER.format(workflow=workflow))
    return owner


def namespace_refusal(db: sqlite3.Connection, namespace: str, holder: str, agent: str) -> str:
    """The §5 text for a namespace that another agent holds."""
    owned = [row[0] for row in db.execute("SELECT namespace FROM measure_namespaces WHERE owner_thread_id=? ORDER BY namespace", (agent,))]
    mine = f"you can emit {', '.join(f'{ns}.*' for ns in owned)} only. Use your own namespace, or pick a new one in measures.yaml." if owned else "you own no namespace yet. Pick a new one in measures.yaml and run measure_sync."
    return f'Namespace "{namespace}" belongs to another agent (thread {holder}); {mine}'


def _no_longer_declared(connection: sqlite3.Connection, owner: str, path: str, kept: set[tuple[str, str]]) -> list[tuple[str, str, int, str]]:
    """Names this owner last synced from ``path`` that the new file leaves out: next version and last spec."""
    latest: dict[tuple[str, str], tuple[int, str, str, int]] = {}
    for kind, name, version, spec_json, file_path, removed in connection.execute(
        "SELECT kind,name,version,spec_json,file_path,removed FROM measure_definitions WHERE owner_thread_id=? ORDER BY version", (owner,),
    ):
        latest[(kind, name)] = (version, spec_json, file_path, removed)
    return [(kind, name, version + 1, spec_json) for (kind, name), (version, spec_json, file_path, removed) in sorted(latest.items())
            if file_path == path and (kind, name) not in kept and not removed]


def sync(db: sqlite3.Connection, *, owner_thread_id: str, path: str, text: str) -> dict[str, Any]:
    """Validate and atomically persist a declaration file, then refresh the registry."""
    try:
        data = load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"measures.yaml is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("measures.yaml must be a mapping")
    lines = Lines(text)
    namespace, items = validate(data, lines)
    workflow = data.get("workflow")
    if workflow and _workflow_owner(path, workflow) != owner_thread_id:
        raise ValueError(f"measure_sync: workflow {workflow} is owned by another agent; only its owner can sync it.")
    if workflow:
        items = [(kind, name, {**spec, "_workflow": workflow} if kind == "event" else spec) for kind, name, spec in items]
    sha = hashlib.sha256(text.encode()).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    changes: list[dict[str, str]] = []
    with write_transaction(db, scope="measure_sync") as connection:
        existing = connection.execute("SELECT owner_thread_id FROM measure_namespaces WHERE namespace=?", (namespace,)).fetchone()
        if existing and existing[0] != owner_thread_id:
            raise ValueError(namespace_refusal(connection, namespace, existing[0], owner_thread_id))
        connection.execute("INSERT OR IGNORE INTO measure_namespaces(namespace,owner_thread_id) VALUES (?,?)", (namespace, owner_thread_id))
        for kind, name, spec in items:
            normalized = json.dumps(spec, sort_keys=True, separators=(",", ":"))
            row = connection.execute("SELECT version,spec_json,removed FROM measure_definitions WHERE owner_thread_id=? AND kind=? AND name=? ORDER BY version DESC LIMIT 1", (owner_thread_id, kind, name)).fetchone()
            if row and row[1] == normalized and not row[2]:
                changes.append({"name": name, "status": "unchanged"})
                continue
            version = 1 if row is None else row[0] + 1
            connection.execute(_INSERT, (owner_thread_id, kind, name, version, normalized, path, sha, now, 0))
            fresh = row is None or bool(row[2])
            changes.append({"name": name, "status": "added" if fresh else f"changed v{version - 1}→v{version}"})
        for kind, name, version, last_spec in _no_longer_declared(connection, owner_thread_id, path, {(kind, name) for kind, name, _ in items}):
            connection.execute(_INSERT, (owner_thread_id, kind, name, version, last_spec, path, sha, now, 1))  # keeps the last real spec
            changes.append({"name": name, "status": "removed"})
    REGISTRY.rebuild(db)
    return {"namespace": namespace, "items": changes}
