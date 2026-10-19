"""The one admission path for declared measure events: script lines and OTLP records both land here."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from host_access.credential_sanitizer import contains_credential
from infra.measure_registry import REGISTRY, DeclaredEvent, namespace_refusal
from infra.outcome_event_attributes import AttributeSpec
from infra.outcome_event_input import OutcomeInputRefusal
from infra.outcome_events import RecordResult, count_outcome_event, record_outcome_event

_STAMPED = ("run_id", "workflow_ref", "workflow_version")
_KIND_RULES = {
    "id": "1 to 200 characters from letters, digits and _ . : # / @ + -, and not a credential",
    "login": "1 to 200 characters from letters, digits and _ . : # / @ + -, and not a credential",
    "count": "a whole number, 0 or more",
    "num": "a finite number, 0 or more, up to 6 decimals",
}


@dataclass(frozen=True)
class Admission:
    result: RecordResult | None
    refusal: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Claims:
    """The ``corsia.*`` values an outside emitter stated. The server checks them and never trusts them."""

    agent_thread_id: str | None = None
    project_id: str | None = None
    run_id: str | None = None
    workflow_ref: str | None = None
    workflow_version: str | None = None
    subject: str | None = None


def admit(
    db: sqlite3.Connection, *, owner_thread_id: str, event_name: str, attributes: dict[str, Any],
    source: str = "agent", provenance: str = "agent_recorded", project_id: str | None = None,
    run_id: str | None = None, workflow: str | None = None, claims: Claims | None = None,
    ts: str | None = None,
) -> Admission:
    """Admit one record for the stamped agent, or count and explain its refusal.

    ``run_id`` and ``workflow`` are the runner's own stamps (script path). ``claims`` are an
    emitter's ``corsia.*`` values (OTLP path), checked against the agent's identity first.
    """
    if not REGISTRY.loaded:
        REGISTRY.rebuild(db)
    refuse = _Refuser(db, owner_thread_id, event_name)
    entry = REGISTRY.events.get(event_name)
    if entry is None:
        names = sorted(name for name, declared in REGISTRY.events.items() if declared.owner == owner_thread_id)
        return refuse("name_undeclared", f'"{event_name}" is not declared in your measures.yaml (declared: {", ".join(names) or "none"}); the record was dropped. Declare it and run measure_sync.')
    if entry.owner != owner_thread_id:
        return refuse("namespace_not_owned", f"{namespace_refusal(db, event_name.split('.', 1)[0], entry.owner, owner_thread_id)} The record was dropped.")
    mismatch, stamps = _identity(db, owner_thread_id, project_id, claims or Claims())
    if mismatch:
        return refuse("identity_mismatch", mismatch)
    if run_id:
        stamps["run_id"] = run_id
    unversioned = bool(workflow) and REGISTRY.workflows.get(workflow) != owner_thread_id
    if workflow and not unversioned:
        stamps["workflow_ref"] = workflow
    stripped = [key for key in attributes if key in _STAMPED]
    subject = (claims.subject if claims else None) or attributes.get("corsia.subject")
    merged = {**{k: v for k, v in attributes.items() if isinstance(k, str) and not k.startswith("corsia.") and k not in _STAMPED}, **stamps}
    late = _time_problem(event_name, ts, entry)
    if late:
        return refuse("record_refused", late)
    key, problem = _dedupe_key(event_name, entry, merged)
    if problem:
        return refuse("record_refused", problem)
    subject_key = subject if isinstance(subject, str) and AttributeSpec("id").accepts(subject) else None
    try:
        result = record_outcome_event(
            db, dedupe_key=key, event_name=event_name, source=source, provenance=provenance,
            ts=ts or datetime.now(timezone.utc).isoformat(), subject_key=subject_key,
            project_id=project_id, agent_thread_id=owner_thread_id, attribution="caller", attributes=merged,
        )
    except OutcomeInputRefusal as exc:
        text = f"{event_name}: attributes exceed 4096 bytes after filtering; the record was dropped. Send fewer or shorter attributes." if "exceed" in str(exc) else f"{event_name}: {exc}; the record was dropped."
        return refuse("record_refused", text)
    if result.inserted and stripped:
        count_outcome_event(db, "attribute_dropped", event_name, len(stripped), agent_thread_id=owner_thread_id)
    if result.inserted and unversioned:
        count_outcome_event(db, "unversioned", event_name, agent_thread_id=owner_thread_id)  # the schedule's workflow is no longer the agent's
    dropped = (*stripped, *result.dropped_keys) if result.inserted else ()
    notes = tuple(_drop_text(event_name, entry, name, attributes.get(name)) for name in dropped)
    for note in notes:
        REGISTRY.note(owner_thread_id, note)  # in memory: a rolled-back settle leaves harmless notes
    return Admission(result, None, notes)


def refuse_line(db: sqlite3.Connection, owner: str, text: str, *, event_name: str, count: int = 1) -> None:
    """Count and note a measure line that was refused before it reached admission (a malformed line, or past the cap)."""
    count_outcome_event(db, "record_refused", event_name, count, agent_thread_id=owner)
    REGISTRY.note(owner, text)  # in memory only: a rolled-back settle leaves a harmless note


def workflow_is_owned(db: sqlite3.Connection, owner: str, workflow: str) -> bool:
    """True when ``workflow`` is one of the agent's own synced workflows."""
    if not REGISTRY.loaded:
        REGISTRY.rebuild(db)
    return REGISTRY.workflows.get(workflow) == owner


def workflow_refusal(db: sqlite3.Connection, owner: str, workflow: str) -> str | None:
    """The text refusing a schedule's ``workflow`` that is unknown or not the agent's, naming the ones it owns."""
    if workflow_is_owned(db, owner, workflow):
        return None
    mine = ", ".join(sorted(ref for ref, holder in REGISTRY.workflows.items() if holder == owner)) or "none"
    why = "is owned by another agent" if workflow in REGISTRY.workflows else "is not declared by any synced measures.yaml"
    return f'workflow "{workflow}" {why}; the schedule was not registered. Use a workflow you own (yours: {mine}), or leave workflow out.'


class _Refuser:
    """Counts a refusal once and keeps its text for ``measure_status``."""

    def __init__(self, db: sqlite3.Connection, owner: str, event_name: str) -> None:
        self.db, self.owner, self.event_name = db, owner, event_name

    def __call__(self, counter: str, text: str) -> Admission:
        count_outcome_event(self.db, counter, self.event_name, agent_thread_id=self.owner)
        REGISTRY.note(self.owner, text)
        return Admission(None, text)


def _identity(db: sqlite3.Connection, owner: str, project_id: str | None, claims: Claims) -> tuple[str | None, dict[str, str]]:
    """Check an emitter's claimed identity; return a refusal, or the stamps the server takes from them."""
    if claims.agent_thread_id and claims.agent_thread_id != owner:
        return (f"corsia.agent_thread_id says {claims.agent_thread_id}, but your key belongs to {owner}; the record was dropped. Remove corsia.agent_thread_id, or use the key minted for {claims.agent_thread_id}.", {})
    if claims.project_id and project_id and claims.project_id != project_id:
        return (f"corsia.project_id says {claims.project_id}, but your key belongs to project {project_id}; the record was dropped. Remove corsia.project_id, or use the key minted for {claims.project_id}.", {})
    stamps: dict[str, str] = {}
    node = _plan_node(db, claims.run_id) if claims.run_id else None
    if node and not _owns_plan(db, owner, node[2], node[3]):
        return (f"corsia.run_id names plan node {claims.run_id} on another agent's plan; the record was dropped. Use a node from your own plan or a worker you spawned, or leave run_id out.", {})
    if claims.run_id:
        stamps["run_id"] = claims.run_id
    if node:
        stamps.update({key: value for key, value in (("workflow_ref", node[0]), ("workflow_version", node[1])) if value})
    elif claims.workflow_ref:
        if REGISTRY.workflows.get(claims.workflow_ref) != owner:
            return (f'corsia.workflow.ref "{claims.workflow_ref}" is owned by another agent; the record was dropped. Use a workflow you own (yours: {", ".join(sorted(w for w, o in REGISTRY.workflows.items() if o == owner)) or "none"}), or leave the ref out.', {})
        stamps["workflow_ref"] = claims.workflow_ref
        if claims.workflow_version:
            stamps["workflow_version"] = claims.workflow_version
    return None, stamps


def _plan_node(db: sqlite3.Connection, node_id: str) -> tuple[str | None, str | None, str, str] | None:
    row = db.execute(
        "SELECT n.workflow_ref, n.workflow_version, p.owner_kind, p.owner_id FROM plan_nodes n JOIN plans p ON p.id = n.plan_id WHERE n.id = ?",
        (node_id,),
    ).fetchone()
    return None if row is None else (row[0], row[1], row[2], row[3])


def _owns_plan(db: sqlite3.Connection, agent: str, owner_kind: str, owner_id: str) -> bool:
    """A plan counts as the agent's own, or as a temporary worker's it spawned."""
    if owner_kind != "thread":
        return False
    return owner_id == agent or db.execute("SELECT 1 FROM threads WHERE id = ? AND spawned_by = ?", (owner_id, agent)).fetchone() is not None


def _time_problem(event_name: str, ts: str | None, entry: DeclaredEvent) -> str | None:
    if ts is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if now - timedelta(days=entry.keep_days) <= parsed <= now + timedelta(minutes=5):
            return None
    except ValueError:
        pass
    return f"{event_name}: time {ts} is outside the window (not older than {entry.keep_days} days, not more than 5 minutes ahead); the record was dropped. Send a time inside the window, or leave time out to use now."


def _dedupe_key(event_name: str, entry: DeclaredEvent, attributes: dict[str, Any]) -> tuple[str, str | None]:
    parts: list[str] = []
    bad: list[str] = []
    problems: list[str] = []
    for key in entry.dedupe:
        value = attributes.get(key)
        spec = entry.specs.get(key)
        if value is None or spec is None or not spec.accepts(value):
            bad.append(f'"{key}"')
            problems.append(f'"{key}" is {"missing" if value is None else "not valid"}')
        parts.append(str(value))
    if not bad:
        return f"{event_name}:{'|'.join(parts)}", None
    needs = " and ".join([", ".join(entry.dedupe[:-1]), entry.dedupe[-1]] if len(entry.dedupe) > 1 else entry.dedupe)
    return "", f"{event_name}: dedupe needs {needs}; {', '.join(problems)}; the record was dropped. Send {', '.join(bad)} with the record."


def _shown(value: Any) -> str:
    text = str(value)
    return "[credential]" if contains_credential(text) else text[:60]


def _drop_text(event_name: str, entry: DeclaredEvent, key: str, value: Any) -> str:
    """The §5 text for one attribute the filter dropped."""
    if key in _STAMPED:
        return f'{event_name}: attribute "{key}" is stamped by the server; the attribute was dropped. Remove it from the record.'
    declared = entry.declared.get(key)
    if declared is None:
        return f'{event_name}: attribute "{key}" is not declared; the attribute was dropped. Declare it on the event and run measure_sync, or stop sending it.'
    if isinstance(declared, dict):
        allowed = ", ".join(declared["enum"])
        return f'{event_name}: attribute "{key}" value "{_shown(value)}" is not one of {allowed}; the attribute was dropped. Send one of those values, or add "{_shown(value)}" to the enum and run measure_sync.'
    return f'{event_name}: attribute "{key}" value "{_shown(value)}" is not a valid {declared} ({_KIND_RULES[declared]}); the attribute was dropped. Send a plain {declared}.'
