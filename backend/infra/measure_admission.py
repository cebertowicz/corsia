"""The one admission path for declared measure events; a script run's lines land here."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from host_access.credential_sanitizer import contains_credential
from infra.measure_registry import REGISTRY, DeclaredEvent, namespace_refusal
from infra.measure_validation import STAMPED_ATTRIBUTES
from infra.outcome_event_input import OutcomeInputRefusal
from infra.outcome_events import RecordResult, count_outcome_event, record_outcome_event

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


def admit(
    db: sqlite3.Connection, *, owner_thread_id: str, event_name: str, attributes: dict[str, Any],
    project_id: str | None = None, run_id: str | None = None, workflow: str | None = None, ts: str | None = None,
) -> Admission:
    """Admit one script-run record for the stamped agent, or count and explain its refusal.

    ``run_id`` and ``workflow`` are the runner's own stamps; the agent can't set them.
    """
    state = REGISTRY.ensure_loaded(db)
    refuse = _Refuser(db, owner_thread_id, event_name)
    entry = state.events.get(event_name)
    if entry is None:
        names = sorted(name for name, declared in state.events.items() if declared.owner == owner_thread_id)
        return refuse("name_undeclared", f'"{event_name}" is not declared in your measures.yaml (declared: {", ".join(names) or "none"}); the record was dropped. Declare it and run measure_sync.')
    if entry.owner != owner_thread_id:
        return refuse("namespace_not_owned", f"{namespace_refusal(db, event_name.split('.', 1)[0], entry.owner, owner_thread_id)} The record was dropped.")
    stamps: dict[str, str] = {}
    if run_id:
        stamps["run_id"] = run_id
    unversioned = bool(workflow) and state.workflows.get(workflow) != owner_thread_id
    if workflow and not unversioned:
        stamps["workflow_ref"] = workflow
    stripped = [key for key in attributes if key in STAMPED_ATTRIBUTES]
    merged = {**{k: v for k, v in attributes.items() if k not in STAMPED_ATTRIBUTES}, **stamps}
    late = _time_problem(event_name, ts, entry)
    if late:
        return refuse("record_refused", late)
    key, problem = _dedupe_key(event_name, entry, merged)
    if problem:
        return refuse("record_refused", problem)
    try:
        result = record_outcome_event(
            db, dedupe_key=key, event_name=event_name, source="agent", provenance="agent_recorded",
            ts=ts or datetime.now(timezone.utc).isoformat(), project_id=project_id,
            agent_thread_id=owner_thread_id, attribution="caller", attributes=merged, declared_specs=entry.specs,
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
    return REGISTRY.ensure_loaded(db).workflows.get(workflow) == owner


def workflow_refusal(db: sqlite3.Connection, owner: str, workflow: str) -> str | None:
    """The text refusing a schedule's ``workflow`` that is unknown or not the agent's, naming the ones it owns."""
    workflows = REGISTRY.ensure_loaded(db).workflows
    if workflows.get(workflow) == owner:
        return None
    mine = ", ".join(sorted(ref for ref, holder in workflows.items() if holder == owner)) or "none"
    why = "is owned by another agent" if workflow in workflows else "is not declared by any synced measures.yaml"
    return f'workflow "{workflow}" {why}; the schedule was not registered. Use a workflow you own (yours: {mine}), or leave workflow out.'


class _Refuser:
    """Counts a refusal once and keeps its text for ``measure_status``."""

    def __init__(self, db: sqlite3.Connection, owner: str, event_name: str) -> None:
        self.db, self.owner, self.event_name = db, owner, event_name

    def __call__(self, counter: str, text: str) -> Admission:
        count_outcome_event(self.db, counter, self.event_name, agent_thread_id=self.owner)
        REGISTRY.note(self.owner, text)
        return Admission(None, text)


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
    if key in STAMPED_ATTRIBUTES:
        return f'{event_name}: attribute "{key}" is stamped by the server; the attribute was dropped. Remove it from the record.'
    declared = entry.declared.get(key)
    if declared is None:
        return f'{event_name}: attribute "{key}" is not declared; the attribute was dropped. Declare it on the event and run measure_sync, or stop sending it.'
    if isinstance(declared, dict):
        allowed = ", ".join(declared["enum"])
        return f'{event_name}: attribute "{key}" value "{_shown(value)}" is not one of {allowed}; the attribute was dropped. Send one of those values, or add "{_shown(value)}" to the enum and run measure_sync.'
    return f'{event_name}: attribute "{key}" value "{_shown(value)}" is not a valid {declared} ({_KIND_RULES[declared]}); the attribute was dropped. Send a plain {declared}.'
