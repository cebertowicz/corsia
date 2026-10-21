"""Deliver a platform-produced event to its owner: wake, digest or dead-letter.

The delivery entrypoint for events the platform itself observes (a poller,
not a webhook); unlike the webhook router, it honors resolved dispositions. It reuses the existing path: ``normalize`` applies the type's
declared rules, ``resolve_targets`` picks the owner, then each target gets an
owner wake (``enqueue_event_wake``) when the event is actionable, otherwise an
owner digest note (``append_thread_context_note``) collapsed by the type's
declared ``collapse_fact``. An unresolvable target goes to the dead-letter
sink. Everything is keyed on ``event_id`` (per target), so a repeat is a no-op.
It names no connector (the no-bespoke-router guard keeps it that way).

Rules agents wrote (``events.rules``) add to that decision after it is made, on
this path and on the webhook router's: each recipient a rule adds is delivered
here, whatever the event's source.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any, Callable

from events import dead_letter, rules
from events.connectors import EventTypeDecl, get_manifest
from events.delivery import enqueue_event_wake
from events.envelope import EventEnvelope, normalize
from events.resolve import Target, resolve_targets
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db_unless_owned
from infra.passive_changes import append_thread_context_note
from infra.product_events import emit_product_event

WOKE = "woke"
DIGESTED = "digested"
DEAD_LETTERED = "dead_lettered"
DUPLICATE = "duplicate"
FILTERED = "filtered"

_NOTE_LINE_LIMIT = 2000


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
    decl: EventTypeDecl, notes: tuple[str, ...] = (), capped: tuple[rules.Entry, ...] = (),
) -> PlatformDelivery:
    """Deliver one target. ``notes`` are the writers' own rule notes, never event text;
    ``capped`` are the rules whose wake the hourly cap turned into this digest."""
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
        wake_details: dict[str, Any] = {**envelope.facts, "event_id": event_id}
        if notes:
            wake_details["rule_notes"] = list(notes)
        enqueue_event_wake(
            thread, envelope.type, _summary(envelope), wake_details,
            message_id=message_id, db=db,
        )
        return PlatformDelivery(event_id, thread, WOKE)
    if _digest_seen(db, thread, event_id):
        return PlatformDelivery(event_id, thread, DUPLICATE)
    # The person's chip keeps the plain event line; the facts are the agent's.
    details = {**_digest_details(envelope, decl.collapse_fact), "chip": _summary(envelope)}
    line = _note_line(envelope, decl.digest_line_facts)
    if capped:
        # R9: one note per capped rule and thread; the latest carries the count.
        details["path"] = "rule:" + ",".join(sorted(entry.rule_id for entry in capped))
        line += "".join(f"; {entry.since_wake} since rule {entry.rule_id} last woke you"
                        for entry in capped)
    if notes:
        details["rule_notes"] = list(notes)
        line += "".join(f"; rule note: {note}" for note in notes)
    line = _clip(line, _NOTE_LINE_LIMIT)
    append_thread_context_note(db, thread_id=thread, summary=line, details=details)
    return PlatformDelivery(event_id, thread, DIGESTED)


def rule_event(
    envelope: EventEnvelope, watch_event: str | None = None, *, ends_artifact: bool = False,
) -> rules.RuleEvent:
    """The envelope as rules see it; ``watch_event`` is the name watches use for it."""
    return rules.RuleEvent(
        envelope.event_id, envelope.connector or "", envelope.type,
        watch_event or envelope.type, envelope.facts, envelope.artifact_ref, ends_artifact,
    )


_RULE_OUTCOMES = {WOKE: rules.WOKE, DIGESTED: rules.DIGESTED, DUPLICATE: rules.DUPLICATE}


def _rule_target(thread: str, delivery: rules.Delivery, routed: Target | None) -> Target:
    """A ridden routed target keeps its own fields; a wake a rule asked for beats its digest."""
    if routed is None:
        return Target(thread, "rule", "rule",
                      disposition="owner_wake" if delivery.wake else "owner_digest")
    if delivery.wake and routed.disposition != "owner_wake":
        return Target(routed.thread, routed.resolution, routed.relationship, routed.reason,
                      routed.routing_state, routed.repair_artifact, "owner_wake")
    return routed


def _resolve_watches(db: sqlite3.Connection, event: rules.RuleEvent, thread: str) -> None:
    """A watch on this event ends once a rule delivered it; Plans owns the settlement."""
    ref = event.artifact_ref
    if not ref:
        return
    from events.identity import RoutingIdentity
    from plans.public import resolve_delivered_event

    resolve_delivered_event(
        RoutingIdentity(ref["integration"], ref["type"], ref["external_id"]),
        event.watch_event, recipient=thread, db=db,
    )


def settle_rule_delivery(
    db: sqlite3.Connection, plan: rules.RulePlan, thread: str, delivery: rules.Delivery,
    made: str, *, ride: bool, resolve: bool = True,
) -> list[rules.Outcome]:
    """Each rule's outcome for one delivery it rode or added, and the watches it ends."""
    outcome = _RULE_OUTCOMES.get(made, rules.DEAD_LETTERED)
    if resolve and outcome in (rules.WOKE, rules.DIGESTED):
        _resolve_watches(db, plan.event, thread)
    return delivery.outcomes(thread, outcome, ride=ride)


def deliver_rule_adds(
    db: sqlite3.Connection, envelope: EventEnvelope, decl: EventTypeDecl, plan: rules.RulePlan,
) -> tuple[list[PlatformDelivery], list[rules.Outcome]]:
    """Deliver each recipient a rule added, whatever the event's source."""
    deliveries, outcomes = [], []
    for thread, delivery in plan.adds.items():
        made = _deliver_target(db, envelope, _rule_target(thread, delivery, None), decl,
                               tuple(delivery.notes), tuple(delivery.capped))
        deliveries.append(made)
        outcomes += settle_rule_delivery(db, plan, thread, delivery, made.outcome, ride=False)
    return deliveries, outcomes


def emit_route_events(envelope: EventEnvelope, deliveries: Iterable[PlatformDelivery]) -> None:
    """Record the same route event for deliveries from either event path."""
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


def apply_webhook_rules_sync(
    envelope: EventEnvelope, watch_event: str, routed_thread: str | None, *, ends_artifact: bool,
) -> rules.RulePlan | None:
    """Rules for one webhook event: deliver what they add, return what rides.

    The router keeps its own delivery to ``routed_thread``; this returns the plan
    so it can drop that delivery or add the rules' notes to it. ``None`` when
    routing chose no thread: rules can then only add recipients. One read when no
    rule for the type matches; the write transaction opens only when one does.
    """
    event = rule_event(envelope, watch_event, ends_artifact=ends_artifact)
    reader = get_db()
    try:
        if not any(rules.matches(rule, event.facts)
                   for rule in rules.lookup(reader, event.connector, event.event_type)):
            return None
    finally:
        reader.close()
    decl = get_manifest(event.connector).decl(event.event_type)
    with write_transaction(None, scope="apply_webhook_rules") as conn:
        routed = [rules.Routed(routed_thread, wake=True)] if routed_thread is not None else []
        plan = rules.evaluate(conn, event, routed)
        if plan is None:
            return None
        added, outcomes = deliver_rule_adds(conn, envelope, decl, plan)
        rules.record(conn, event, [*plan.settled, *outcomes])
        emit_route_events(envelope, added)
        return plan


def record_rule_ride_sync(plan: rules.RulePlan, thread: str, delivered: bool) -> None:
    """Record the rules that rode the router's own delivery; it resolved the watches."""
    delivery = plan.rides.get(thread)
    if delivery is None:
        return
    with write_transaction(None, scope="record_rule_ride") as conn:
        outcomes = settle_rule_delivery(
            conn, plan, thread, delivery, WOKE if delivered else DEAD_LETTERED,
            ride=True, resolve=False)
        rules.record(conn, plan.event, outcomes)


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
    decl = get_manifest(connector).decl(envelope.type)
    with write_transaction(db, scope="deliver_platform_event") as conn:
        targets = resolve_targets(envelope, repair_owner=repair_owner, db=conn)
        filtered = [
            target_filter(target, conn) if target_filter is not None else None
            for target in targets
        ]
        plan = rules.evaluate(conn, rule_event(envelope), [
            rules.Routed(target.thread, target.disposition == "owner_wake",
                         owner=target.relationship == "owner")
            for target, reason in zip(targets, filtered)
            if reason is None and target.thread is not None and target.disposition != "dead_letter"
        ])
        deliveries = [
            PlatformDelivery(envelope.event_id, target.thread, FILTERED, reason)
            if reason is not None
            else _deliver_routed(conn, envelope, target, decl, plan)
            for target, reason in zip(targets, filtered)
        ]
        if plan is not None:
            outcomes = list(plan.settled)
            for made in deliveries:
                if (made.thread_id is not None and made.thread_id in plan.rides
                        and made.outcome != FILTERED):
                    outcomes += settle_rule_delivery(
                        conn, plan, made.thread_id, plan.rides[made.thread_id], made.outcome,
                        ride=True)
            added, added_outcomes = deliver_rule_adds(conn, envelope, decl, plan)
            deliveries += added
            rules.record(conn, plan.event, [*outcomes, *added_outcomes])
        emit_route_events(envelope, deliveries)
        return deliveries


def _deliver_routed(
    db: sqlite3.Connection, envelope: EventEnvelope, target: Target, decl: EventTypeDecl,
    plan: rules.RulePlan | None,
) -> PlatformDelivery:
    """Today's delivery to one routed target, unless a rule dropped or rides it."""
    thread = target.thread
    if plan is None or thread is None:
        return _deliver_target(db, envelope, target, decl)
    if thread in plan.drops:
        return PlatformDelivery(envelope.event_id, thread, FILTERED,
                                f"dropped by rule {plan.drops[thread]}")
    ride = plan.rides.get(thread)
    if ride is None:
        return _deliver_target(db, envelope, target, decl)
    return _deliver_target(db, envelope, _rule_target(thread, ride, target), decl,
                           tuple(ride.notes))


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
