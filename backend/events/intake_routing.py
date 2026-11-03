"""Who a declared event reaches by rule 0, and the watches that delivery ends."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import replace

from events.disposition import delivery_disposition
from events.envelope import EventEnvelope
from events.platform import DIGESTED, WOKE, PlatformDelivery, resolve_watches, rule_event
from events.resolve import Target, resolve_targets
from events.rule_zero import UNOWNED


def declarer_targets(owner: str, envelope: EventEnvelope, conn) -> list[Target]:
    """Routing for a declared type nothing ran for (an agent's emit, a scheduled script): its
    declaring thread is the registered owner, so rules read that owner from the routing state
    whether it is live or dead-lettered."""
    from events.delivery import thread_exists_and_active

    state = {"owner": {"parent_thread_id": owner}}
    if thread_exists_and_active(owner, db=conn):
        return [Target(
            owner, "declared_owner", "owner", routing_state=state,
            disposition=delivery_disposition("owner", envelope.event_class),
        )]
    return [Target(None, "dead_letter", "none", reason="owner_inactive", routing_state=state,
                   disposition="dead_letter")]


def artifact_targets(owner: str, envelope: EventEnvelope, conn) -> list[Target]:
    """Routing for a declared type a rule's run emitted on its artifact: that artifact's
    registered owner, as for any event on it. An artifact with no owner (an outside
    contributor's PR) delivers it to no one: the declarer stays the scope its rule set's
    own rules fire in; its routing state names no owner, so a ``registered_owner`` rule reaches
    no one, and ``UNOWNED`` marks the target so rule 0 holds its delivery."""
    routed = resolve_targets(envelope, db=conn)
    if not any(target.no_owner for target in routed):
        return routed
    return [replace(target, resolution=UNOWNED, routing_state={}) for target in declarer_targets(owner, envelope, conn)]


def resolve_delivered_watches(
    conn: sqlite3.Connection, envelope: EventEnvelope, deliveries: Iterable[PlatformDelivery],
) -> None:
    """End each recipient's watch on this event once it reached them (Plans owns the settlement)."""
    event = rule_event(envelope)
    for delivery in deliveries:
        if delivery.thread_id is not None and delivery.outcome in (WOKE, DIGESTED):
            resolve_watches(conn, event, delivery.thread_id)
