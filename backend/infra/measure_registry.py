"""Declared Agent KPI measures: the in-process registry and the ``measure_sync`` loader."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from infra.database import write_transaction
from infra.measure_validation import STAMPED_ATTRIBUTES, Lines, keep_days, load, validate
from infra.outcome_event_attributes import AttributeSpec

REFUSAL_LIMIT = 20
_INSERT = (
    "INSERT INTO measure_definitions (owner_thread_id,kind,name,version,spec_json,file_path,file_sha256,synced_at,removed) "
    "VALUES (?,?,?,?,?,?,?,?,?)"
)
# The owner's rows, one PK index range; the latest version per name is picked in Python.
_OWNER_ROWS = "SELECT kind,name,version,spec_json,removed,file_path FROM measure_definitions WHERE owner_thread_id=?"
# The one read inside the sync transaction: every event row in a namespace, a range on idx_measure_definitions_kind_name.
NAMESPACE_ROWS = (
    "SELECT owner_thread_id,name,version,spec_json,removed,file_path,file_sha256 FROM measure_definitions "
    "WHERE kind='event' AND name>=? AND name<?"
)
_CONFLICT = "measure_sync: another sync of this file ran at the same time; run measure_sync again."
# Why an agent may not use a workflow; each caller says what was refused.
_NO_OWNER = "workflow {workflow} has no owner. Add `owner: <your thread id>` under `workflow:` in its corsia.yaml."
_NOT_EFFECTIVE = "workflow {workflow} is not one of your workflow skills (yours: {mine}). Name a workflow skill you own."
_OTHER_OWNER = "workflow {workflow} is owned by another agent (thread {owner}); only its owner can use it."
_KEY_CONFLICT = (
    "workflow {workflow} is in thread {owner}'s own skills, but its owner key names thread {named}; "
    "only {owner} can use it. Remove the owner key, or set it to {owner}."
)
_NOT_BESIDE = "measure_sync: workflow {workflow}'s measures.yaml syncs only from beside its SKILL.md ({folder}/measures.yaml), not {path}."


@dataclass(frozen=True)
class WorkflowOwner:
    """One effective workflow skill as the owner rule reads it (built by ``tools.workflow_owners``).

    ``owner`` is the skill's thread for a thread-scope skill and its ``workflow.owner`` key
    otherwise; ``named`` is a thread-scope skill's owner key when it names another thread.
    """

    skill_dir: Path
    owner: str | None
    named: str | None = None
    skill: Any = None


def owned_by(workflows: Mapping[str, WorkflowOwner], agent: str) -> list[str]:
    """The effective workflows ``agent`` owns, by name."""
    return sorted(name for name, entry in workflows.items() if entry.owner == agent and entry.named is None)


def workflow_problem(workflows: Mapping[str, WorkflowOwner], agent: str, workflow: str) -> str | None:
    """Why ``agent`` may not use ``workflow``, or None when it owns it."""
    entry = workflows.get(workflow)
    if entry is None:
        return _NOT_EFFECTIVE.format(workflow=workflow, mine=", ".join(owned_by(workflows, agent)) or "none")
    if entry.named is not None:
        return _KEY_CONFLICT.format(workflow=workflow, owner=entry.owner, named=entry.named)
    if entry.owner is None:
        return _NO_OWNER.format(workflow=workflow)
    if entry.owner != agent:
        return _OTHER_OWNER.format(workflow=workflow, owner=entry.owner)
    return None


@dataclass(frozen=True)
class DeclaredEvent:
    owner: str
    specs: dict[str, AttributeSpec]
    dedupe: tuple[str, ...]
    declared: dict[str, Any]
    keep_days: int
    use: str = "record"
    since: dict[str, Any] | None = None


@dataclass(frozen=True)
class RegistryState:
    """One complete read of the declarations; the registry swaps whole states, never edits one."""

    events: dict[str, DeclaredEvent] = field(default_factory=dict)

    def namespaces_of(self, agent: str) -> list[str]:
        """The namespaces ``agent`` holds: the ones its live events are in."""
        return sorted({name.split(".", 1)[0] for name, event in self.events.items() if event.owner == agent})


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
_STAMPED_SPECS = {
    "run_id": AttributeSpec("id"),
    "workflow_ref": AttributeSpec("id"),
    # The resolver's version: a Git SHA (with a dirty mark) or a System manifest id, longer than an id allows.
    "workflow_version": AttributeSpec("text", max_chars=400),
}
assert set(_STAMPED_SPECS) == set(STAMPED_ATTRIBUTES)


def _read_state(db: sqlite3.Connection) -> RegistryState:
    events: dict[str, DeclaredEvent] = {}
    rows = db.execute(
        "SELECT owner_thread_id,name,spec_json FROM measure_definitions "
        "WHERE kind='event' AND removed=0 AND (owner_thread_id,name,version) IN "
        "(SELECT owner_thread_id,name,max(version) FROM measure_definitions "
        "WHERE kind='event' GROUP BY owner_thread_id,name)"
    )
    for owner, name, raw in rows:
        try:
            events[name] = _declared(owner, json.loads(raw))
        except (KeyError, TypeError, ValueError) as exc:
            # One bad stored spec is skipped, so it can't stop intake for every other agent.
            logger.warning("measure registry skipped {} of {}: {}", name, owner, exc)
    return RegistryState(events=events)


def _declared(owner: str, spec: dict[str, Any]) -> DeclaredEvent:
    attributes = spec["attributes"]
    return DeclaredEvent(
        owner, {**_attribute_specs(attributes), **_STAMPED_SPECS},
        tuple(spec["dedupe"]), attributes, keep_days(spec), spec.get("use", "record"), spec.get("since"),
    )


def _attribute_specs(raw: dict[str, Any]) -> dict[str, AttributeSpec]:
    result: dict[str, AttributeSpec] = {}
    for name, kind in raw.items():
        result[name] = AttributeSpec(kind) if isinstance(kind, str) else AttributeSpec("enum", frozenset(kind["enum"]))
    return result


def _check_agent_file(path: str, agent: str) -> None:
    """An agent-level file syncs only from the caller's own .threads folder."""
    folder = Path(path).parent
    if Path(path).name != "measures.yaml" or folder.name != agent or folder.parent.name != ".threads":
        raise ValueError(
            f"measure_sync: an agent-level measures.yaml syncs only from .threads/{agent}/measures.yaml, not {path}. "
            "Move it there, or add workflow: <skill name> if it belongs to a workflow skill you own."
        )


def _check_workflow_file(path: str, workflow: str, agent: str, workflows: Mapping[str, WorkflowOwner]) -> None:
    """A workflow file syncs only for its owner, from beside that workflow's SKILL.md."""
    problem = workflow_problem(workflows, agent, workflow)
    if problem:
        raise ValueError(f"measure_sync: {problem} Its measures.yaml was not synced; keep agent-level measures in your own .threads folder.")
    folder = workflows[workflow].skill_dir
    if Path(path).resolve().parent != folder.resolve():
        raise ValueError(_NOT_BESIDE.format(workflow=workflow, folder=folder, path=path))


def workflow_for_file(path: str, agent: str, workflows: Mapping[str, WorkflowOwner]) -> str | None:
    """Resolve a synced file through the same live ownership and path checks as sync."""
    try:
        _check_agent_file(path, agent)
        return None
    except ValueError:
        pass
    for workflow in workflows:
        try:
            _check_workflow_file(path, workflow, agent, workflows)
            return workflow
        except ValueError:
            continue
    return None


def namespace_refusal(namespace: str, holder: str, held: list[str]) -> str:
    """The §5 text for a namespace that another agent holds; ``held`` is the caller's own namespaces."""
    mine = f"you can emit {', '.join(f'{ns}.*' for ns in held)} only. Use your own namespace, or pick a new one in measures.yaml." if held else "you own no namespace yet. Pick a new one in measures.yaml and run measure_sync."
    return f'Namespace "{namespace}" belongs to another agent (thread {holder}); {mine}'


Latest = dict[tuple[str, str], tuple[int, str, bool, str]]


def _latest(rows: Any) -> Latest:
    """The latest version of each (kind, name) among ``rows``, as (version, spec, removed, file)."""
    latest: Latest = {}
    for kind, name, version, spec, removed, file in rows:
        if (kind, name) not in latest or version > latest[(kind, name)][0]:
            latest[(kind, name)] = (version, spec, bool(removed), file)
    return latest


def _plan(latest: Latest, owner: str, path: str, items: list[tuple[str, str, str]], sha: str, now: str) -> tuple[list[tuple[Any, ...]], list[dict[str, str]]]:
    """The rows to insert and every item's status, from one read of the owner's latest versions."""
    inserts: list[tuple[Any, ...]] = []
    changes: list[dict[str, str]] = []
    for kind, name, normalized in items:
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


def _holder_is_gone(connection: sqlite3.Connection, thread_id: str) -> bool:
    """True when the holder's thread no longer exists, was purged, or the person archived it."""
    row = connection.execute("SELECT lifecycle_state, archived_reason FROM threads WHERE id=?", (thread_id,)).fetchone()
    return row is None or row[0] == "purged" or (row[0] == "archived" and row[1] == "user")


def _claim_namespace(connection: sqlite3.Connection, namespace: str, owner: str, held: list[str], now: str) -> tuple[list[tuple[Any, ...]], list[str]]:
    """Refuse a namespace another live agent holds; retire a departed holder's live events in it.

    Holding is derived from live event definitions, so a namespace frees when its holder's file
    stops declaring it. Returns the retirement rows and a line per takeover for the sync result.
    """
    rows = connection.execute(NAMESPACE_ROWS, (f"{namespace}.", f"{namespace}/")).fetchall()  # "/" sorts right after "."
    latest: dict[tuple[str, str], tuple[Any, ...]] = {}
    for row in rows:
        holder, name, version = row[0], row[1], row[2]
        if holder != owner and ((holder, name) not in latest or version > latest[(holder, name)][2]):
            latest[(holder, name)] = row
    live: dict[str, list[tuple[Any, ...]]] = {}
    for (holder, _name), row in sorted(latest.items()):
        if not row[4]:
            live.setdefault(holder, []).append(row)
    retire: list[tuple[Any, ...]] = []
    taken: list[str] = []
    for holder, events in live.items():
        if not _holder_is_gone(connection, holder):
            raise ValueError(namespace_refusal(namespace, holder, held))
        retire += [(holder, "event", name, version + 1, spec, file, sha, now, 1) for _, name, version, spec, _, file, sha in events]
        taken.append(f"took over namespace {namespace} from {holder}; {len(events)} events retired")
    return retire, taken


def persist_declaration(db: sqlite3.Connection, owner: str, path: str, namespace: str, items: list[tuple[str, str, str]], sha: str, version_attr: str | None = None) -> dict[str, Any]:
    """Plan from one read outside the transaction, then write the finished rows in one short transaction."""
    now = datetime.now(timezone.utc).isoformat()
    latest = _latest(db.execute(_OWNER_ROWS, (owner,)))
    inserts, changes = _plan(latest, owner, path, items, sha, now)
    held = sorted({name.split(".", 1)[0] for (kind, name), row in latest.items() if kind == "event" and not row[2]} - {namespace})
    with write_transaction(db, scope="measure_sync") as connection:
        retire, taken = _claim_namespace(connection, namespace, owner, held, now)
        connection.executemany(_INSERT, inserts + retire)
        connection.execute("UPDATE measure_definitions SET version_attr=? WHERE owner_thread_id=? AND file_path=?",
                           (version_attr, owner, path))
    result: dict[str, Any] = {"namespace": namespace, "items": changes}
    if taken:
        result["namespace_takeover"] = taken
    return result


def sync(
    db: sqlite3.Connection, *, owner_thread_id: str, path: str, text: str,
    workflows: Mapping[str, WorkflowOwner] | None = None,
) -> dict[str, Any]:
    """Validate and persist a declaration file, then refresh the registry.

    ``workflows`` is the caller's effective workflow skills under the owner rule; a file that
    names a workflow syncs only when the caller owns it. A version conflict with a concurrent
    sync re-plans once, then refuses.
    """
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
        _check_workflow_file(path, workflow, owner_thread_id, workflows or {})
    else:
        _check_agent_file(path, owner_thread_id)
        for kind, name, spec in items:
            if kind == 'kpi':
                if spec['formula'] == 'rate_per_run':
                    raise ValueError(f'kpi {name}: rate_per_run requires a workflow')
                for field in ('num', 'den'):
                    if 'builtin' in spec.get(field, {}):
                        raise ValueError(f'kpi {name}: {field}.builtin requires a workflow')
    sha = hashlib.sha256(text.encode()).hexdigest()
    normalized = [(kind, name, json.dumps(spec, sort_keys=True, separators=(",", ":"))) for kind, name, spec in items]
    for attempt in (1, 2):
        try:
            result = persist_declaration(db, owner_thread_id, path, namespace, normalized, sha, data.get("version_attr"))
            break
        except sqlite3.IntegrityError:
            if attempt == 2:
                raise ValueError(_CONFLICT) from None
    REGISTRY.rebuild(db)
    return result
