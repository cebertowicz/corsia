"""The one admission path for declared measure events; a script run's lines land here."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

from host_access.credential_sanitizer import contains_credential
from infra.measure_registry import REGISTRY, DeclaredEvent, namespace_refusal
from infra.measure_validation import STAMPED_ATTRIBUTES
from infra.outcome_event_input import OutcomeInputRefusal
from infra.outcome_events import RecordResult, count_outcome_event, record_outcome_event

# Counter labels for names an agent didn't declare: a printed name never becomes a counter row,
# so an agent's counter rows per day stay within (declared events + these labels) x counters.
UNNAMED_LINE = "(measure line)"
UNDECLARED = "(undeclared)"
NOT_YOURS = "(not yours)"
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
    project_id: str | None = None, run_id: str | None = None, workflow_ref: str | None = None,
    workflow_version: str | None = None, ts: str | None = None,
) -> Admission:
    """Admit one script-run record for the stamped agent, or count and explain its refusal.

    ``run_id``, ``workflow_ref`` and ``workflow_version`` are the runner's own stamps, resolved
    before settling; the agent can't set them.
    """
    state = REGISTRY.ensure_loaded(db)
    entry = state.events.get(event_name)
    if entry is None:
        names = sorted(name for name, declared in state.events.items() if declared.owner == owner_thread_id)
        return _refuse(db, owner_thread_id, UNDECLARED, "name_undeclared", f'"{_shown(event_name)}" is not declared in your measures.yaml (declared: {", ".join(names) or "none"}); the record was dropped. Declare it and run measure_sync.')
    if entry.owner != owner_thread_id:
        refusal = namespace_refusal(event_name.split(".", 1)[0], entry.owner, state.namespaces_of(owner_thread_id))
        return _refuse(db, owner_thread_id, NOT_YOURS, "namespace_not_owned", f"{refusal} The record was dropped.")
    refuse = partial(_refuse, db, owner_thread_id, event_name)
    stamps = {key: value for key, value in (("run_id", run_id), ("workflow_ref", workflow_ref), ("workflow_version", workflow_version)) if value}
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
    dropped = (*stripped, *result.dropped_keys) if result.inserted else ()
    notes = tuple(_drop_text(event_name, entry, name, attributes.get(name)) for name in dropped)
    for note in notes:
        REGISTRY.note(owner_thread_id, note)  # in memory: a rolled-back settle leaves harmless notes
    return Admission(result, None, notes)


def refuse_line(db: sqlite3.Connection, owner: str, text: str, *, event_name: str, count: int = 1) -> None:
    """Count and note a measure line that was refused before it reached admission (a malformed line, or past the cap).

    The counter carries ``event_name`` only when the owner declared it, else :data:`UNNAMED_LINE`.
    """
    entry = REGISTRY.ensure_loaded(db).events.get(event_name)
    label = event_name if entry is not None and entry.owner == owner else UNNAMED_LINE
    count_outcome_event(db, "record_refused", label, count, agent_thread_id=owner)
    REGISTRY.note(owner, text)  # in memory only: a rolled-back settle leaves a harmless note


def _refuse(db: sqlite3.Connection, owner: str, label: str, counter: str, text: str) -> Admission:
    """Count a refusal once under ``label`` and keep its text for ``measure_status``."""
    count_outcome_event(db, counter, label, agent_thread_id=owner)
    REGISTRY.note(owner, text)
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
