"""Agent-written event rules on a GitHub webhook, once routing has decided.

The router's own decision stays as it is; this runs at the end of its
preparation and reads that decision (the routed thread and the routing state it
resolved) rather than resolving anything again. The event is the connector's
own ``normalize`` of the payload, as the manifest type that declares this
webhook's name; a check suite's pull request is the one routing selected.
Recipients a rule adds are delivered through the platform path
(``events.platform``). For the routed thread a rule can only drop the delivery
(its writer's own) or ride it with a note. When routing chose no thread, rules
can only add recipients, and today's result stands. Comments have no declared
type yet, so no rule matches them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from events.connectors.github import CHECK_SUITE_PR, MANIFEST
from events.envelope import normalize
from events.github_mapper import ends_artifact
from events.identity_formula import MalformedEventError
from events.platform import apply_webhook_rules_sync, record_rule_ride_sync
from events.resolve import Target
from infra.product_events import emit_product_event

if TYPE_CHECKING:
    from events.connectors.github_route_preparation import PreparedRoute

def routed_targets(
    thread: str | None, wake_reason: str | None, routing_state: dict[str, Any],
) -> list[Target]:
    """The router's decision as Targets: it wakes the thread it chose (``wake_reason``
    is its relationship: owner, triage or tag), and parks the event otherwise."""
    if thread is None:
        return [Target(None, "dead_letter", "none", routing_state=routing_state,
                       disposition="dead_letter")]
    return [Target(thread, wake_reason or "owner", wake_reason or "owner",
                   routing_state=routing_state, disposition="owner_wake")]


def apply_routed_rules(
    prepared: PreparedRoute, internal_event: str, payload: dict[str, Any],
    selected_pr: dict[str, Any] | None, routing_state: dict[str, Any],
    wake_reason: str | None = None,
) -> None:
    """Deliver what rules add, then drop or annotate the routed delivery in ``prepared``.

    ``prepared.thread_id`` is ``None`` when routing chose no thread; ``prepared.result``
    then already holds today's result, which rules never change.
    """
    event_type = MANIFEST.routed_type(internal_event)
    if event_type is None:
        return
    try:
        envelope = normalize(MANIFEST.connector, payload, event_type,
                             resolved={CHECK_SUITE_PR: selected_pr})
    except MalformedEventError:
        return
    thread = prepared.thread_id
    plan = apply_webhook_rules_sync(
        envelope, internal_event, routed_targets(thread, wake_reason, routing_state),
        ends_artifact=ends_artifact(internal_event))
    if plan is None or thread is None:
        return
    if thread in plan.drops:
        _drop(prepared, internal_event, f"dropped by rule {plan.drops[thread]}")
        return
    ride = plan.rides.get(thread)
    if ride is None:
        return
    prepared.rule_plan = plan
    if ride.notes:
        # The writers' own notes ride the routed wake; nothing else changes.
        prepared.details = {**(prepared.details or {}), "rule_notes": list(ride.notes)}


def _drop(prepared: PreparedRoute, internal_event: str, reason: str) -> None:
    """The routed thread wrote a rule dropping this event for itself."""
    from events.connectors import github_pr_notice
    from events.router import RouteOutcome, RouteResult

    if prepared.notice:
        github_pr_notice.release_notice(prepared.thread_id, prepared.identity)
        prepared.notice = None
    external_id = prepared.identity.external_id if prepared.identity else None
    emit_product_event(
        "event_route", RouteOutcome.SUPPRESSED.value,
        properties={"internal_event": internal_event, "thread_id": prepared.thread_id,
                    "external_id": external_id, "reason": reason},
    )
    prepared.result = RouteResult(RouteOutcome.SUPPRESSED, thread_id=prepared.thread_id,
                                  external_id=external_id, reason=reason)


def record_routed_rides(prepared: PreparedRoute, delivered: bool) -> None:
    """Record the rules that rode the routed delivery; the router resolved the watches."""
    if prepared.rule_plan is not None and prepared.thread_id is not None:
        record_rule_ride_sync(prepared.rule_plan, prepared.thread_id, delivered)
