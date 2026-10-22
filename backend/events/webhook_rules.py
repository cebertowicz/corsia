"""Agent-written rules on a webhook event, once the webhook router has decided.

The router keeps its own delivery to the thread it chose; these add the
recipients rules add (delivered through the platform path, ``events.platform``)
and record the rules that rode its delivery. Names no connector.
"""

from __future__ import annotations

from dataclasses import replace

from loguru import logger

from events import rule_reads, rules
from events.connectors import get_manifest
from events.envelope import EventEnvelope
from events.platform import (
    DEAD_LETTERED, WOKE, deliver_rule_adds, emit_route_events, rule_event, settle_rule_delivery,
)
from events.resolve import Target
from events.rule_match import matches, repository_fact
from infra.database import get_db, write_transaction


def apply_webhook_rules_sync(
    envelope: EventEnvelope, watch_event: str, targets: list[Target], *, ends_artifact: bool,
    repository_projects: frozenset[str] | None = frozenset(),
) -> rules.RulePlan | None:
    """Rules for one webhook event: deliver what they add, return what rides.

    ``targets`` are the router's own decision; it keeps its delivery to the routed
    thread, and this returns the plan so it can drop that delivery or add the
    rules' notes to it. One read when no rule for the type in the event's project
    matches; the write transaction opens only when one does. With no owner and
    nothing routed, only ``repository_projects`` (``rule_reads.unrouted_projects``,
    read before routing's preparation gate) scope the rules; ``None`` means that
    lookup failed, and no rule fires.
    """
    event = rule_event(envelope, watch_event, ends_artifact=ends_artifact)
    anchor = rules.routed(targets)[2]
    if anchor is None:
        if repository_projects is None:
            _lookup_failed(event)
            return None
        if not repository_projects:
            return None
        event = replace(event, repository_projects=repository_projects)
    reader = get_db()
    try:
        if not any(matches(rule, event.facts) for rule in rules.lookup(
                reader, event.connector, event.event_type, anchor, event.repository_projects)):
            return None
    finally:
        reader.close()
    decl = get_manifest(event.connector).decl(event.event_type)
    with write_transaction(None, scope="apply_webhook_rules") as conn:
        plan = rules.evaluate(conn, event, targets)
        if plan is None:
            return None
        added, outcomes = deliver_rule_adds(conn, envelope, decl, plan)
        rules.record(conn, event, [*plan.settled, *outcomes])
        emit_route_events(envelope, added)
        return plan


def _lookup_failed(event: rules.RuleEvent) -> None:
    """Fail closed, as ``create`` does: no self-rule fires when the projects tracking
    the event's repository can't be read, and each one that matched says why."""
    repository = event.facts.get(repository_fact(event.connector) or "")
    logger.warning("event rules: repository lookup failed for {} {} on {}; no self-rule fired",
                   event.connector, event.event_type, repository)
    with write_transaction(None, scope="apply_webhook_rules") as conn:
        rules.record(conn, event, [
            rules.Outcome(rule.id, rule.created_by, rules.DEAD_LETTERED, reason=rules.LOOKUP_FAILED)
            for rule in map(rules.from_row, rule_reads.self_rules(
                conn, event.connector, event.event_type))
            if matches(rule, event.facts) and rules.self_on_repository(rule)
        ])


def record_rule_ride_sync(plan: rules.RulePlan, thread: str, delivered: bool) -> None:
    """Record the rules that rode the router's own delivery; it resolved the watches."""
    delivery = plan.rides.get(thread)
    if delivery is None:
        return
    with write_transaction(None, scope="record_rule_ride") as conn:
        outcomes = settle_rule_delivery(
            conn, plan, thread, delivery, WOKE if delivered else DEAD_LETTERED, ride=True)
        rules.record(conn, plan.event, outcomes)
