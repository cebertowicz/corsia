"""Deliver a platform-produced event to its owner: wake, digest or dead-letter.

The delivery entrypoint for events the platform itself observes (a poller,
not a webhook); unlike the webhook router, it honors resolved dispositions. It reuses the existing path: ``normalize`` applies the type's
declared rules, ``resolve_targets`` picks the owner, then each target gets an
owner wake (``enqueue_event_wake``) when the event is actionable, otherwise an
owner digest note (``append_thread_context_note``) collapsed by the type's
declared ``collapse_fact``. An unresolvable target goes to the dead-letter
sink. Everything is keyed on ``event_id`` (per target), so a repeat is a no-op.
It names no connector (the no-bespoke-router guard keeps it that way).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from events import dead_letter
from events.connectors import get_manifest
from events.delivery import enqueue_event_wake
from events.envelope import EventEnvelope, normalize
from events.resolve import Target, resolve_targets
from infra.database import write_transaction
from infra.db_async import run_in_db_unless_owned
from infra.passive_changes import append_thread_context_note
from infra.product_events import emit_product_event

WOKE = "woke"
DIGESTED = "digested"
DEAD_LETTERED = "dead_lettered"
DUPLICATE = "duplicate"
FILTERED = "filtered"


@dataclass(frozen=True)
class PlatformDelivery:
    """What happened to one resolved target."""

    event_id: str
    thread_id: str | None
    outcome: str  # woke | digested | dead_lettered | duplicate | filtered
    reason: str | None = None


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
    value = envelope.facts.get(collapse_fact) if collapse_fact else None
    if value is not None:
        details["path"] = str(value)
    return details


def _summary(envelope: EventEnvelope) -> str:
    return f"{envelope.type} from {envelope.provenance.get('source', 'platform')}"


def _deliver_target(
    db: sqlite3.Connection, envelope: EventEnvelope, target: Target,
    collapse_fact: str | None,
) -> PlatformDelivery:
    event_id = envelope.event_id
    disposition = target.disposition
    thread = target.thread
    if thread is None or disposition == "dead_letter":
        if _parked(db, envelope):
            return PlatformDelivery(event_id, None, DUPLICATE)
        reason = target.reason or target.resolution
        dead_letter.park(
            envelope.type,
            {"event_id": event_id, "connector": envelope.connector,
             "facts": envelope.facts},
            reason, event_id, db=db,
        )
        return PlatformDelivery(event_id, None, DEAD_LETTERED, reason)
    if disposition == "owner_wake":
        message_id = _wake_message_id(event_id, thread)
        if db.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone():
            return PlatformDelivery(event_id, thread, DUPLICATE)
        enqueue_event_wake(
            thread, envelope.type, _summary(envelope),
            {**envelope.facts, "event_id": event_id},
            message_id=message_id, db=db,
        )
        return PlatformDelivery(event_id, thread, WOKE)
    if _digest_seen(db, thread, event_id):
        return PlatformDelivery(event_id, thread, DUPLICATE)
    append_thread_context_note(
        db, thread_id=thread, summary=_summary(envelope),
        details=_digest_details(envelope, collapse_fact),
    )
    return PlatformDelivery(event_id, thread, DIGESTED)


def deliver_platform_event_sync(
    connector: str,
    raw_payload: dict[str, Any],
    event_type: str | None = None,
    *,
    repair_owner: str | None = None,
    target_filter: Callable[[Target, sqlite3.Connection], str | None] | None = None,
    db: sqlite3.Connection | None = None,
) -> list[PlatformDelivery]:
    """Normalize, resolve and deliver one event in one write transaction."""
    envelope = normalize(connector, raw_payload, event_type)
    collapse_fact = get_manifest(connector).decl(envelope.type).collapse_fact
    with write_transaction(db, scope="deliver_platform_event") as conn:
        targets = resolve_targets(envelope, repair_owner=repair_owner, db=conn)
        deliveries = [
            PlatformDelivery(envelope.event_id, target.thread, FILTERED, reason)
            if target_filter is not None and (reason := target_filter(target, conn)) is not None
            else _deliver_target(conn, envelope, target, collapse_fact)
            for target in targets
        ]
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
