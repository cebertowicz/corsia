"""Declared Agent KPI measures: the in-process registry and the ``measure_sync`` loader."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from infra.database import write_transaction
from infra.measure_validation import STAMPED_ATTRIBUTES, Lines, load, validate
from infra.outcome_event_attributes import AttributeSpec

REFUSAL_LIMIT = 20
_INSERT = (
    "INSERT INTO measure_definitions (owner_thread_id,kind,name,version,spec_json,file_path,file_sha256,synced_at,removed) "
    "VALUES (?,?,?,?,?,?,?,?,?)"
)
_LATEST = (
    "SELECT d.kind,d.name,d.version,d.spec_json,d.removed,d.file_path FROM measure_definitions d "
    "WHERE d.owner_thread_id=? AND d.version=(SELECT max(version) FROM measure_definitions "
    "WHERE owner_thread_id=d.owner_thread_id AND kind=d.kind AND name=d.name)"
)
_NO_OWNER = (
    "measure_sync: workflow {workflow} has no owner, so its measures.yaml can't be synced. "
    "Add `corsia: {{workflow: {{owner: <your thread id>}}}}` to its SKILL.md frontmatter, "
    "or keep your measures in your own .threads folder."
)


@dataclass(frozen=True)
class DeclaredEvent:
    owner: str
    specs: dict[str, AttributeSpec]
    dedupe: tuple[str, ...]
    declared: dict[str, Any]
    keep_days: int


@dataclass(frozen=True)
class RegistryState:
    """One complete read of the declarations; the registry swaps whole states, never edits one."""

    events: dict[str, DeclaredEvent] = field(default_factory=dict)
    namespaces: dict[str, str] = field(default_factory=dict)
    workflows: dict[str, str] = field(default_factory=dict)


class MeasureRegistry:
    """The declared-event registry; a committed sync swaps it, and the first use after boot loads it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: RegistryState | None = None
        self.refusals: dict[str, deque[str]] = {}

    @property
    def loaded(self) -> bool:
        return self._state is not None

    @property
    def events(self) -> dict[str, DeclaredEvent]:
        return (self._state or RegistryState()).events

    @property
    def namespaces(self) -> dict[str, str]:
        return (self._state or RegistryState()).namespaces

    @property
    def workflows(self) -> dict[str, str]:
        return (self._state or RegistryState()).workflows

    def ensure_loaded(self, db: sqlite3.Connection) -> RegistryState:
        """The current state, loading it first when nothing has; callers that read twice keep the one state."""
        state = self._state
        if state is not None:
            return state
        with self._lock:
            if self._state is None:
                self._state = _read_state(db)
            return self._state

    def rebuild(self, db: sqlite3.Connection) -> None:
        """Read the declarations off to the side, then swap them in whole; readers never take the lock."""
        with self._lock:
            self._state = _read_state(db)

    def reset(self) -> None:
        """Forget the loaded state and refusal notes (tests, between disposable databases)."""
        with self._lock:
            self._state = None
        self.refusals.clear()

    def note(self, owner: str, text: str) -> None:
        """Keep the agent's last refusal texts in memory only; a restart loses them."""
        self.refusals.setdefault(owner, deque(maxlen=REFUSAL_LIMIT)).append(text)


REGISTRY = MeasureRegistry()
_STAMPED_SPECS = {name: AttributeSpec("id") for name in STAMPED_ATTRIBUTES}


def _read_state(db: sqlite3.Connection) -> RegistryState:
    events: dict[str, DeclaredEvent] = {}
    rows = db.execute(
        "SELECT owner_thread_id,name,spec_json FROM measure_definitions "
        "WHERE kind='event' AND removed=0 AND (owner_thread_id,name,version) IN "
        "(SELECT owner_thread_id,name,max(version) FROM measure_definitions "
        "WHERE kind='event' GROUP BY owner_thread_id,name)"
    )
    for owner, name, raw in rows:
        spec = json.loads(raw)
        specs = {**_attribute_specs(spec["attributes"]), **_STAMPED_SPECS}
        events[name] = DeclaredEvent(owner, specs, tuple(spec["dedupe"]), spec["attributes"], int(spec.get("keep_days", 400)))
    return RegistryState(
        events=events,
        namespaces=dict(db.execute("SELECT namespace, owner_thread_id FROM measure_namespaces")),
        workflows=dict(db.execute("SELECT workflow, owner_thread_id FROM measure_workflows")),
    )


def _attribute_specs(raw: dict[str, Any]) -> dict[str, AttributeSpec]:
    result: dict[str, AttributeSpec] = {}
    for name, kind in raw.items():
        result[name] = AttributeSpec(kind) if isinstance(kind, str) else AttributeSpec("enum", frozenset(kind["enum"]))
    return result


def _skill_frontmatter(path: str) -> dict[str, Any]:
    """The frontmatter of the SKILL.md beside a declaration file; empty when there is none."""
    try:
        meta = yaml.safe_load((Path(path).parent / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[1])
    except (IndexError, OSError, yaml.YAMLError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _check_workflow_file(path: str, workflow: str, agent: str) -> None:
    """A workflow file syncs only when its sibling SKILL.md is named for it and names the caller as owner."""
    meta = _skill_frontmatter(path)
    corsia = meta.get("corsia")
    declared = corsia.get("workflow") if isinstance(corsia, dict) else None
    owner = declared.get("owner") if isinstance(declared, dict) else None
    if not isinstance(owner, str) or not owner:
        raise ValueError(_NO_OWNER.format(workflow=workflow))
    if owner != agent:
        raise ValueError(f"measure_sync: workflow {workflow} is owned by another agent; only its owner can sync it.")
    if meta.get("name") != workflow:
        raise ValueError(
            f"measure_sync: workflow {workflow} must be the name of the skill it sits in (this SKILL.md is named {meta.get('name')}). "
            "Change workflow: to the skill's name."
        )


def _check_agent_file(path: str, agent: str) -> None:
    """An agent-level file syncs only from the caller's own .threads folder."""
    folder = Path(path).parent
    if Path(path).name != "measures.yaml" or folder.name != agent or folder.parent.name != ".threads":
        raise ValueError(
            f"measure_sync: an agent-level measures.yaml syncs only from .threads/{agent}/measures.yaml, not {path}. "
            "Move it there, or add workflow: <skill name> if it belongs to a skill you own."
        )


def namespace_refusal(db: sqlite3.Connection, namespace: str, holder: str, agent: str) -> str:
    """The §5 text for a namespace that another agent holds."""
    owned = [row[0] for row in db.execute("SELECT namespace FROM measure_namespaces WHERE owner_thread_id=? ORDER BY namespace", (agent,))]
    mine = f"you can emit {', '.join(f'{ns}.*' for ns in owned)} only. Use your own namespace, or pick a new one in measures.yaml." if owned else "you own no namespace yet. Pick a new one in measures.yaml and run measure_sync."
    return f'Namespace "{namespace}" belongs to another agent (thread {holder}); {mine}'


def _plan(
    latest: dict[tuple[str, str], tuple[int, str, bool, str]], owner: str, path: str, items: list[tuple[str, str, dict[str, Any]]],
    sha: str, now: str,
) -> tuple[list[tuple[Any, ...]], list[dict[str, str]]]:
    """The rows to insert and every item's status, from one read of the owner's latest versions."""
    inserts: list[tuple[Any, ...]] = []
    changes: list[dict[str, str]] = []
    for kind, name, spec in items:
        normalized = json.dumps(spec, sort_keys=True, separators=(",", ":"))
        row = latest.get((kind, name))
        if row and row[1] == normalized and not row[2]:
            changes.append({"name": name, "status": "unchanged"})
            continue
        version = 1 if row is None else row[0] + 1
        inserts.append((owner, kind, name, version, normalized, path, sha, now, 0))
        changes.append({"name": name, "status": "added" if row is None or row[2] else f"changed v{row[0]}→v{version}"})
    declared = {(kind, name) for kind, name, _ in items}
    for (kind, name), (version, last_spec, removed, file) in sorted(latest.items()):
        if file == path and not removed and (kind, name) not in declared:
            inserts.append((owner, kind, name, version + 1, last_spec, path, sha, now, 1))  # keeps the last real spec
            changes.append({"name": name, "status": "removed"})
    return inserts, changes


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
    if workflow:
        _check_workflow_file(path, workflow, owner_thread_id)
    else:
        _check_agent_file(path, owner_thread_id)
    sha = hashlib.sha256(text.encode()).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    latest = {(kind, name): (version, spec, bool(removed), file) for kind, name, version, spec, removed, file in db.execute(_LATEST, (owner_thread_id,))}
    inserts, changes = _plan(latest, owner_thread_id, path, items, sha, now)
    with write_transaction(db, scope="measure_sync") as connection:
        holder = connection.execute("SELECT owner_thread_id FROM measure_namespaces WHERE namespace=?", (namespace,)).fetchone()
        if holder and holder[0] != owner_thread_id:
            raise ValueError(namespace_refusal(connection, namespace, holder[0], owner_thread_id))
        if workflow:
            connection.execute("INSERT OR IGNORE INTO measure_workflows(workflow,owner_thread_id) VALUES (?,?)", (workflow, owner_thread_id))
            claimed = connection.execute("SELECT owner_thread_id FROM measure_workflows WHERE workflow=?", (workflow,)).fetchone()[0]
            if claimed != owner_thread_id:
                raise ValueError(f"measure_sync: workflow {workflow} is already owned by another agent (thread {claimed}); use a skill name of your own.")
        connection.execute("INSERT OR IGNORE INTO measure_namespaces(namespace,owner_thread_id) VALUES (?,?)", (namespace, owner_thread_id))
        connection.executemany(_INSERT, inserts)
    REGISTRY.rebuild(db)
    return {"namespace": namespace, "items": changes}
