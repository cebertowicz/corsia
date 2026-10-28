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
this path, intake's (``deliver_envelope_sync``) and the webhook router's
(``events.webhook_rules``): each recipient a rule adds passes the caller's own
target filter and is delivered here, whatever the event's source.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from collections.abc import Iterable
from typing import Any, Callable

from loguru import logger

from events import dead_letter, rules
from events.connectors import EventTypeDecl, get_manifest
from events.delivery import enqueue_event_wake
from events.disposition import delivery_disposition
from events.envelope import EventEnvelope, normalize
from events.resolve import Target, lifecycle, resolve_targets
from infra.database import write_transaction
from infra.db_async import run_in_db_unless_owned
from infra.passive_changes import append_thread_context_note
from infra.product_events import emit_product_event
from infra.send_types import SendRejected

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
    decl: EventTypeDecl, notes: tuple[str, ...] = (),
) -> PlatformDelivery:
    """Deliver one target. ``notes`` are the writers' own rule notes, never event text."""
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
        trace=tuple(envelope.provenance.get("rule_trace") or ()), requester=_requester(envelope),
        origin=envelope.origin,
    )


def _requester(envelope: EventEnvelope) -> str | None:
    """The agent a CORSIA-produced event answers, from its manifest's declared fact; intake
    admits that namespace from CORSIA alone, so no producer can name one."""
    try:
        fact = get_manifest(envelope.connector or "").requester_fact
    except KeyError:
        return None
    value = envelope.facts.get(fact) if fact else None
    return str(value) if value else None


_RULE_OUTCOMES = {WOKE: rules.WOKE, DIGESTED: rules.DIGESTED, DUPLICATE: rules.DUPLICATE}
Filter = Callable[[Target, sqlite3.Connection], str | None]


def _rule_target(
    thread: str, delivery: rules.Delivery, routed: Target | None, plan: rules.RulePlan,
    event_class: str,
) -> Target:
    """The disposition table's answer for a delivery rules add or ride; a ridden
    target keeps its own fields, and a wake a rule's guards allowed beats its digest."""
    if routed is None:
        return Target(thread, "rule", "rule", disposition=delivery_disposition(
            "rule", event_class, plan.lifecycle, rule_wake=delivery.wake))
    if not delivery.wake or routed.disposition == "owner_wake":
        return routed
    state = lifecycle(routed.routing_state) if routed.routing_state else plan.lifecycle
    return replace(routed, disposition=delivery_disposition(
        routed.relationship, event_class, state, rule_wake=True))


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
    made: str, *, ride: bool,
) -> list[rules.Outcome]:
    """Each rule's outcome for one delivery it rode or added. A delivery a rule
    added ends that recipient's watches; routing ends them for one it rode."""
    outcome = _RULE_OUTCOMES.get(made, rules.DEAD_LETTERED)
    if not ride and outcome in (rules.WOKE, rules.DIGESTED):
        _resolve_watches(db, plan.event, thread)
    return delivery.outcomes(thread, outcome, ride=ride)


def deliver_rule_adds(
    db: sqlite3.Connection, envelope: EventEnvelope, decl: EventTypeDecl, plan: rules.RulePlan,
    target_filter: Filter | None = None,
) -> tuple[list[PlatformDelivery], list[rules.Outcome]]:
    """Deliver each recipient a rule added, through the filter the caller applies to
    routed recipients (the mail poller's is the account's reach, R30). Rules only add:
    a recipient refusing it (being deleted, say) rolls back only its own savepoint
    and is its rules' outcome, never a cost to routing's deliveries."""
    deliveries, outcomes = [], []
    for thread, delivery in plan.adds.items():
        target = _rule_target(thread, delivery, None, plan, envelope.event_class)
        reason = target_filter(target, db) if target_filter is not None else None
        if reason is not None:
            deliveries.append(PlatformDelivery(envelope.event_id, thread, FILTERED, reason))
            outcomes += [rules.Outcome(entry.rule_id, thread, rules.HELD, reason=reason)
                         for entry in delivery.entries]
            continue
        try:
            with write_transaction(db, scope="deliver_rule_add"):
                made = _deliver_target(db, envelope, target, decl,
                                       tuple(delivery.notes))
        except SendRejected as exc:
            logger.warning("event rules: {} refused {}: {}", thread, envelope.type, exc)
            deliveries.append(PlatformDelivery(envelope.event_id, thread, DEAD_LETTERED,
                                               rules.UNAVAILABLE))
            outcomes += [rules.Outcome(entry.rule_id, thread, rules.DEAD_LETTERED,
                                       reason=rules.UNAVAILABLE) for entry in delivery.entries]
            continue
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


def deliver_platform_event_sync(
    connector: str,
    raw_payload: dict[str, Any],
    event_type: str | None = None,
    *,
    repair_owner: str | None = None,
    target_filter: Filter | None = None,
    db: sqlite3.Connection | None = None,
) -> list[PlatformDelivery]:
    """Normalize, resolve and deliver one connector event in one write transaction.

    ``target_filter`` names why a recipient may not get this event (``None``: it
    may); it applies alike to routed recipients and to those a rule adds.
    """
    envelope = normalize(connector, raw_payload, event_type)
    deliveries, _ = deliver_envelope_sync(
        envelope, get_manifest(connector).decl(envelope.type),
        lambda conn: resolve_targets(envelope, repair_owner=repair_owner, db=conn),
        target_filter=target_filter, db=db,
    )
    return deliveries


def deliver_envelope_sync(
    envelope: EventEnvelope, decl: EventTypeDecl,
    resolve: Callable[[sqlite3.Connection], list[Target]], *,
    target_filter: Filter | None = None,
    repository_projects: frozenset[str] | None = frozenset(),
    db: sqlite3.Connection | None = None,
) -> tuple[list[PlatformDelivery], rules.RulePlan | None]:
    """Deliver one normalized event to the targets ``resolve`` picks, then apply rules.

    One write transaction; the plan is returned so intake can say which rules acted.
    ``repository_projects`` (``rule_reads.unrouted_projects``, read before the
    transaction) scope the rules when ``resolve`` routes the event to no thread;
    ``None``, that lookup failed, and no rule fires there (``rules.lookup_failed``).
    """
    with write_transaction(db, scope="deliver_platform_event") as conn:
        targets = resolve(conn)
        filtered = [
            target_filter(target, conn) if target_filter is not None else None
            for target in targets
        ]
        skipped = [target.thread for target, reason in zip(targets, filtered) if reason is not None]
        event = rule_event(envelope)
        plan = None
        if repository_projects is None and rules.routed(targets, skipped)[2] is None:
            rules.record(conn, event, rules.lookup_failed(conn, event))
        else:
            event = replace(event, repository_projects=repository_projects or frozenset())
            plan = rules.evaluate(conn, event, targets, skipped=skipped)
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
            added, added_outcomes = deliver_rule_adds(conn, envelope, decl, plan, target_filter)
            deliveries += added
            rules.record(conn, plan.event, [*outcomes, *added_outcomes])
        emit_route_events(envelope, deliveries)
        return deliveries, plan


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
    return _deliver_target(
        db, envelope, _rule_target(thread, ride, target, plan, envelope.event_class), decl,
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
