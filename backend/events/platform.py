"""Deliver a platform-produced event to its owner: wake, digest, drop or dead-letter.

The delivery entrypoint for events the platform itself observes (a poller,
not a webhook). The webhook router wakes its one evaluated recipient itself
and records it here (``settle_routed_delivery``); only this path honors digest
dispositions and more than one recipient. ``normalize`` applies the type's
declared rules, ``evaluate`` applies drop and delivery rules around
``resolve_targets``' owner, then each target gets an owner wake
(``enqueue_event_wake``) when the event is actionable, otherwise an owner
digest note (``append_thread_context_note``) collapsed by the type's
declared ``collapse_fact``. An unresolvable target goes to the dead-letter
sink. Everything is keyed on ``event_id`` (per target), so a repeat is a no-op.
It names no connector (the no-bespoke-router guard keeps it that way).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from typing import Any, Callable

from loguru import logger

from events import dead_letter
from events.connectors import EventTypeDecl, get_manifest
from events.delivery import enqueue_event_wake
from events.envelope import EventEnvelope, normalize
from events.resolve import Target, resolve_targets
from events.rules import Evaluation, evaluate, event_scope
from events import route_decisions
from infra.database import write_transaction
from infra.db_async import run_in_db_unless_owned
from infra.passive_changes import append_thread_context_note
from infra.product_events import emit_product_event

WOKE = "woke"
DIGESTED = "digested"
DEAD_LETTERED = "dead_lettered"
DUPLICATE = "duplicate"
FILTERED = "filtered"
DROPPED = "dropped"

_NOTE_LINE_LIMIT = 2000


@dataclass(frozen=True)
class PlatformDelivery:
    """What happened to one resolved target."""

    event_id: str
    thread_id: str | None
    outcome: str  # woke | digested | dead_lettered | duplicate | filtered | dropped
    reason: str | None = None


def _traced(envelope: EventEnvelope, rule_ids: list[str]) -> EventEnvelope:
    return replace(envelope, provenance={**envelope.provenance, 'rule_trace': rule_ids})


def _after_delivery(db, envelope: EventEnvelope, delivered: PlatformDelivery,
                    rule_ids: list[str], scope: str) -> None:
    """Transport owns the delivery; Plans alone owns watch settlement."""
    route_decisions.record(db, envelope, rule_ids=rule_ids,
                           thread_id=delivered.thread_id, outcome=delivered.outcome, scope=scope)
    if delivered.outcome in (WOKE, DIGESTED) and envelope.artifact_ref:
        from events.identity import RoutingIdentity
        from plans.public import resolve_delivered_event

        ref = envelope.artifact_ref
        resolve_delivered_event(
            RoutingIdentity(ref['integration'], ref['type'], ref['external_id']),
            envelope.type, recipient=delivered.thread_id, db=db,
        )


def record_drop(db, envelope: EventEnvelope, rule_id: str, scope: str,
                *, author: str | None) -> None:
    """The one record of a rule drop: route decision, log line and product event."""
    route_decisions.record(db, envelope, rule_ids=[rule_id], thread_id=None,
                           outcome=DROPPED, scope=scope)
    logger.info("event-routing: rule {} dropped {} (author {})",
                rule_id, envelope.type, author or '?')
    emit_product_event(
        "event_route", "suppressed",
        properties={"internal_event": envelope.type, "author": author, "rule_id": rule_id},
    )


def settle_routed_delivery(envelope: EventEnvelope, thread_id: str) -> None:
    """Record a router wake and settle its watches, as platform delivery does."""
    with write_transaction(None, scope='routed_event_delivered') as conn:
        _after_delivery(conn, envelope, PlatformDelivery(envelope.event_id, thread_id, WOKE),
                        envelope.provenance['rule_trace'], event_scope(envelope, db=conn))


def _wake_message_id(event_id: str, thread_id: str) -> str:
    return f"platform-event:{event_id}:{thread_id}"


def _digest_seen(db: sqlite3.Connection, thread_id: str, event_id: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM context_change_journal WHERE object_kind='thread_context' "
        "AND object_id=? AND json_extract(details_json,'$.event_id')=? LIMIT 1",
        (thread_id, event_id),
    ).fetchone()
    return row is not None


def _parked(db: sqlite3.Connection, envelope: EventEnvelope) -> bool:
    row = db.execute(
        "SELECT 1 FROM dead_letter_events WHERE internal_event=? AND external_id=? "
        "LIMIT 1",
        (envelope.type, envelope.event_id),
    ).fetchone()
    return row is not None


def _digest_details(envelope: EventEnvelope, collapse_fact: str | None) -> dict[str, Any]:
    """Facts for the note, minus ``message_id``, which would defeat collapsing."""
    details: dict[str, Any] = {
        k: v for k, v in envelope.facts.items() if k != "message_id"
    }
    details["event_id"] = envelope.event_id
    details['provenance'] = envelope.provenance
    value = envelope.facts.get(collapse_fact) if collapse_fact else None
    if value is not None:
        details["path"] = str(value)
    return details


def _summary(envelope: EventEnvelope) -> str:
    return f"{envelope.type} from {envelope.provenance.get('source', 'platform')}"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"… [{len(text) - limit} more characters not shown]"
    return text[:limit - len(marker)] + marker


def _note_line(envelope: EventEnvelope, line_facts: tuple[str, ...]) -> str:
    """The agent's note line: the event plus the type's declared line facts.

    Only declared facts show, so outside text such as an email subject never
    reaches the line. The note's details never reach the agent, so values are
    whole; only a line past the cap is cut, and it says how much is missing.
    """
    facts = []
    for key in line_facts:
        value = envelope.facts.get(key)
        if value is None:
            continue
        rendered = value.replace("\n", "\\n") if isinstance(value, str) else json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        facts.append(f"{key}={rendered}")
    line = _summary(envelope)
    if facts:
        line = f"{line}: {', '.join(facts)}"
    return _clip(line, _NOTE_LINE_LIMIT)


def _deliver_target(
    db: sqlite3.Connection, envelope: EventEnvelope, target: Target,
    decl: EventTypeDecl,
) -> PlatformDelivery:
    event_id = envelope.event_id
    disposition = target.disposition
    thread = target.thread
    if thread is None or disposition == "dead_letter":
        if _parked(db, envelope):
            return PlatformDelivery(event_id, thread, DUPLICATE)
        reason = target.reason or target.resolution
        dead_letter.park(
            envelope.type,
            {"event_id": event_id, "connector": envelope.connector,
             "facts": envelope.facts},
            reason, event_id, db=db,
        )
        return PlatformDelivery(event_id, thread, DEAD_LETTERED, reason)
    if disposition == "owner_wake":
        message_id = _wake_message_id(event_id, thread)
        if db.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone():
            return PlatformDelivery(event_id, thread, DUPLICATE)
        enqueue_event_wake(
            thread, envelope.type, _summary(envelope),
            {**envelope.facts, "event_id": event_id},
            message_id=message_id, db=db, metadata={'provenance': envelope.provenance},
        )
        return PlatformDelivery(event_id, thread, WOKE)
    if _digest_seen(db, thread, event_id):
        return PlatformDelivery(event_id, thread, DUPLICATE)
    # The person's chip keeps the plain event line; the facts are the agent's.
    details = {**_digest_details(envelope, decl.collapse_fact), "chip": _summary(envelope)}
    append_thread_context_note(
        db, thread_id=thread,
        summary=_note_line(envelope, decl.digest_line_facts), details=details,
    )
    return PlatformDelivery(event_id, thread, DIGESTED)


def deliver_event_envelope_sync(
    envelope: EventEnvelope, declaration: EventTypeDecl, *,
    resolve_owner: Callable[[], list[Target]] | None = None,
    scope: str | None = None,
    repair_owner: str | None = None,
    target_filter: Callable[[Target, sqlite3.Connection], str | None] | None = None,
    db: sqlite3.Connection | None = None,
) -> tuple[list[PlatformDelivery], Evaluation]:
    """Evaluate and deliver an already-normalized event through the platform path."""
    with write_transaction(db, scope="deliver_platform_event") as conn:
        event_rule_scope = scope or event_scope(envelope, db=conn)
        evaluated = evaluate(
            envelope, scope=event_rule_scope, db=conn,
            resolve_owner=resolve_owner or (lambda: resolve_targets(
                envelope, repair_owner=repair_owner, db=conn)),
        )
        if evaluated.dropped_by:
            record_drop(conn, envelope, evaluated.dropped_by, event_rule_scope,
                        author=envelope.facts.get('author'))
            return [PlatformDelivery(envelope.event_id, None, DROPPED)], evaluated
        deliveries = [
            PlatformDelivery(envelope.event_id, target.thread, FILTERED, reason)
            if target_filter is not None and (reason := target_filter(target, conn)) is not None
            else _deliver_target(conn, _traced(envelope, evaluated.rule_ids[target.thread]), target, declaration)
            for target in evaluated.targets
        ]
        for delivered in deliveries:
            _after_delivery(conn, envelope, delivered, evaluated.rule_ids[delivered.thread_id], event_rule_scope)
        for delivery in deliveries:
            emit_product_event(
                "event_route", delivery.outcome,
                properties={
                    "internal_event": envelope.type,
                    "thread_id": delivery.thread_id,
                    "external_id": delivery.event_id,
                    "reason": delivery.reason,
                },
            )
        return deliveries, evaluated


def deliver_platform_event_sync(
    connector: str,
    raw_payload: dict[str, Any],
    event_type: str | None = None,
    *,
    repair_owner: str | None = None,
    target_filter: Callable[[Target, sqlite3.Connection], str | None] | None = None,
    db: sqlite3.Connection | None = None,
) -> list[PlatformDelivery]:
    """Normalize, evaluate and deliver one connector event through the platform path."""
    envelope = normalize(connector, raw_payload, event_type)
    declaration = get_manifest(connector).decl(envelope.type)
    deliveries, _ = deliver_event_envelope_sync(
        envelope, declaration, repair_owner=repair_owner,
        target_filter=target_filter, db=db,
    )
    return deliveries


async def deliver_platform_event(
    connector: str,
    raw_payload: dict[str, Any],
    event_type: str | None = None,
    *,
    repair_owner: str | None = None,
    db: sqlite3.Connection | None = None,
) -> list[PlatformDelivery]:
    """Async entrypoint for a platform producer (e.g. the email poller).

    Runs the sync delivery on the DB pool; a caller that owns ``db`` runs inline.
    """
    return await run_in_db_unless_owned(
        db, deliver_platform_event_sync, connector, raw_payload, event_type,
        repair_owner=repair_owner, db=db,
    )
