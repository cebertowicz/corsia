"""One server-side admission path for declared routable events."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from events.connectors import EventTypeDecl
from events.disposition import delivery_disposition
from events.envelope import EventEnvelope
from events.platform import deliver_event_envelope_sync
from events.resolve import Target
from infra.database import get_db, write_transaction
from infra.measure_registry import REGISTRY

_MAX_FACT_BYTES = 64 * 1024
_ERROR_FIXES = {
    "facts_not_object": "Pass a JSON object to --facts.",
    "bad_json": "Pass valid JSON to --facts.",
    "too_large": "Send fewer or shorter facts.",
    "undeclared_type": "Use a declared route or both event type.",
    "undeclared_fact": "Use only facts declared for this event type.",
    "bad_fact_kind": "Match each declared fact kind.",
    "not_your_namespace": "Use a type declared by this session's rule set.",
    "reserved_type": "worker.* events are produced by CORSIA only.",
    "intake_unreachable": "Retry with the same --id.",
}


@dataclass(frozen=True)
class IntakeRefusal:
    code: str
    text: str


@dataclass(frozen=True)
class IntakeResult:
    accepted: bool
    duplicate: bool
    refusal: IntakeRefusal | None
    rules_matched: int


@dataclass(frozen=True)
class _ResolvedEvent:
    """One validated event or its refusal, with the platform-delivery inputs."""

    envelope: EventEnvelope | None
    refusal: IntakeRefusal | None
    registered_owner: str | None
    declaration: EventTypeDecl | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emitted_event_id(event_type: str, identity: str | None) -> str:
    """Make optional emitter retry identities unique to their declared type."""
    if not identity:
        return uuid4().hex
    return hashlib.sha256(f"{event_type}\0{identity}".encode()).hexdigest()


def format_event_error(code: str, text: str) -> str:
    """Render the common caller-facing error copy without exposing event facts."""
    return f"{code}: {text.rstrip('.')}. {_ERROR_FIXES.get(code, 'Correct the event and try again.')}"


def _refusal(code: str, text: str) -> IntakeResult:
    return IntakeResult(False, False, IntakeRefusal(code, text), 0)


def _resolution_refusal(code: str, text: str) -> _ResolvedEvent:
    return _ResolvedEvent(None, IntakeRefusal(code, text), None, None)


def _split_type(event_type: str) -> tuple[str, str] | None:
    namespace, separator, suffix = event_type.partition(".")
    return (namespace, suffix) if separator and suffix else None


def resolve_event_type(conn, event_type: str) -> tuple[EventTypeDecl, Any | None] | None:
    """Resolve declared types before connector manifests for every event-facing surface."""
    split = _split_type(event_type)
    if split is None:
        return None
    namespace, suffix = split
    entry = REGISTRY.ensure_loaded(conn).events.get(event_type)
    if entry is not None:
        return EventTypeDecl(suffix, (), tuple(entry.declared)), entry
    from events.connectors import get_manifest

    try:
        return get_manifest(namespace).decl(suffix), None
    except KeyError:
        return None


def _facts_problem(facts: Any) -> IntakeResult | None:
    if not isinstance(facts, dict):
        return _refusal("facts_not_object", "facts must be a JSON object")
    try:
        size = len(json.dumps(facts, separators=(",", ":"), ensure_ascii=False).encode())
    except (TypeError, ValueError):
        return _refusal("bad_fact_kind", "facts must contain JSON values of their declared kinds")
    if size > _MAX_FACT_BYTES:
        return _refusal("too_large", "facts exceed the 64 KB event limit; send fewer or shorter facts")
    return None


def _script_owner(conn, producer: str) -> str | None:
    if not producer.startswith("script:"):
        return None
    row = conn.execute(
        "SELECT thread_id FROM automation_schedules WHERE id=?",
        (producer.removeprefix("script:"),),
    ).fetchone()
    return str(row[0]) if row else None


def _connector_event(conn, event_type: str, facts: dict[str, Any], producer: str) -> _ResolvedEvent:
    from events.connectors import get_manifest

    split = _split_type(event_type)
    if split is None:
        return _resolution_refusal("undeclared_type", "event type is not declared")
    namespace, suffix = split
    try:
        manifest = get_manifest(namespace)
        declaration = manifest.decl(suffix)
    except KeyError:
        return _resolution_refusal("undeclared_type", "event type is not declared")
    if producer != manifest.connector:
        return _resolution_refusal("not_your_namespace", "producer does not own this event namespace")
    allowed = set(declaration.identity_facts) | set(declaration.filterable_fields)
    if next((name for name in facts if name not in allowed), None) is not None:
        return _resolution_refusal("undeclared_fact", "facts include a name not declared for this event type")
    return _ResolvedEvent(
        EventEnvelope(
            event_id="", type=suffix, connector=namespace, facts=facts, relationships=(),
            artifact_ref=manifest.build_artifact_ref(suffix, facts, facts),
            provenance={"source": namespace, "ingested_at": _now(), "rule_trace": []},
            event_class=declaration.event_class, ownerless_ok=declaration.ownerless_ok, origin="connector",
        ),
        None,
        None,
        declaration,
    )


def _declared_event(conn, event_type: str, facts: dict[str, Any], producer: str, origin: str) -> _ResolvedEvent:
    split = _split_type(event_type)
    resolved = resolve_event_type(conn, event_type)
    if split is None or resolved is None:
        return _resolution_refusal("undeclared_type", "event type is not declared")
    namespace, suffix = split
    declaration, entry = resolved
    if entry is None:
        return _resolution_refusal("not_your_namespace", "producer does not own this event namespace")
    owner = _script_owner(conn, producer) if producer.startswith("script:") else producer
    if owner is None or entry.owner != owner:
        return _resolution_refusal("not_your_namespace", "producer does not own this event namespace")
    if entry.use == "record":
        return _resolution_refusal(
            "undeclared_type",
            f"{event_type} is declared use: record (measurement only); declare use: route or both to route it.",
        )
    if next((name for name in facts if name not in entry.declared), None) is not None:
        return _resolution_refusal("undeclared_fact", "facts include a name not declared for this event type")
    if next((name for name, value in facts.items() if not entry.specs[name].accepts(value)), None) is not None:
        return _resolution_refusal("bad_fact_kind", "a fact does not match its declared kind")
    return _ResolvedEvent(
        EventEnvelope(
            event_id="", type=suffix, connector=namespace, facts=facts, relationships=(),
            artifact_ref=None, provenance={"source": namespace, "ingested_at": _now(), "rule_trace": []},
            event_class="informational", ownerless_ok=False, origin=origin,
        ),
        None,
        owner,
        declaration,
    )


def _owner_scope(conn, owner: str) -> str:
    row = conn.execute("SELECT project_id FROM threads WHERE id=?", (owner,)).fetchone()
    return f"project:{row['project_id']}" if row and row["project_id"] else "global"


def _registered_owner_targets(owner: str, envelope: EventEnvelope, conn) -> list[Target]:
    from events.delivery import thread_exists_and_active

    if thread_exists_and_active(owner, db=conn):
        return [Target(
            owner, "declared_owner", "owner",
            disposition=delivery_disposition("owner", envelope.event_class),
        )]
    return [Target(None, "dead_letter", "none", reason="owner_inactive", disposition="dead_letter")]


def _record_acceptance(conn, *, event_id: str, event_type: str, origin: str,
                       producer: str, rules_matched: int) -> None:
    conn.execute(
        "INSERT INTO event_intake_records "
        "(event_id,outcome,rules_matched,producer,origin,event_type,at) VALUES (?,?,?,?,?,?,?)",
        (event_id, "accepted", rules_matched, producer, origin, event_type, _now()),
    )


def record_refusal(*, event_id: str, event_type: str, origin: Literal["emitter", "agent"],
                   producer: str, refusal: IntakeRefusal) -> None:
    """Retain a caller-visible CLI or tool refusal without retaining event facts or text."""
    with write_transaction(None, scope="record_event_refusal") as conn:
        conn.execute(
            "INSERT INTO event_intake_records "
            "(event_id,outcome,rules_matched,refusal_code,producer,origin,event_type,at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (event_id, "refused", 0, refusal.code, producer, origin, event_type, _now()),
        )
        conn.execute(
            "DELETE FROM event_intake_records WHERE id IN "
            "(SELECT id FROM event_intake_records WHERE outcome='refused' AND at < ? ORDER BY id LIMIT 1000)",
            ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),)
        )


def _admit_sync(event_type: str, facts: dict[str, Any], origin: str, producer: str, event_id: str) -> IntakeResult:
    with write_transaction(None, scope="admit_event") as conn:
        row = conn.execute(
            "SELECT rules_matched FROM event_intake_records "
            "WHERE event_id=? AND outcome='accepted'", (event_id,),
        ).fetchone()
        if row is not None:
            return IntakeResult(True, True, None, int(row["rules_matched"]))
        if event_type.startswith("worker.") and origin != "platform":
            return _refusal("reserved_type", "worker events are produced by the platform only")
        resolved = (
            _connector_event(conn, event_type, facts, producer)
            if origin == "connector"
            else _declared_event(conn, event_type, facts, producer, origin)
        )
        if resolved.refusal is not None:
            return IntakeResult(False, False, resolved.refusal, 0)
        if resolved.envelope is None or resolved.declaration is None:
            return _refusal("unresolved_event", "event could not be resolved; this is a platform fault")
        envelope = EventEnvelope(**{**resolved.envelope.__dict__, "event_id": event_id})
        owner = resolved.registered_owner
        deliveries, evaluated = deliver_event_envelope_sync(
            envelope,
            resolved.declaration,
            resolve_owner=(lambda: _registered_owner_targets(owner, envelope, conn)) if owner else None,
            scope=_owner_scope(conn, owner) if owner else None,
            db=conn,
        )
        rule_ids = {rule_id for ids in evaluated.rule_ids.values() for rule_id in ids if rule_id != "builtin:0"}
        if evaluated.dropped_by:
            rule_ids.add(evaluated.dropped_by)
        _record_acceptance(
            conn, event_id=event_id, event_type=event_type, origin=origin,
            producer=producer, rules_matched=len(rule_ids),
        )
        return IntakeResult(True, all(delivery.outcome == "duplicate" for delivery in deliveries), None, len(rule_ids))


async def admit_event(*, event_type: str, facts: dict, origin: Literal["connector", "emitter", "agent", "platform"],
                      producer: str, event_id: str, external_id: str | None = None,
                      occurred_at: datetime | None = None) -> IntakeResult:
    """Validate, deduplicate, evaluate and deliver one declared event without trusting producer input."""
    problem = _facts_problem(facts)
    if problem is not None:
        return problem
    return await asyncio.to_thread(_admit_sync, event_type, facts, origin, producer, event_id)
