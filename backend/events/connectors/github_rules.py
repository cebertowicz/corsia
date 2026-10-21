"""Agent-written event rules on a routed GitHub webhook, once routing chose its thread.

The router's own decision stays as it is; this runs at the end of its
preparation. Recipients a rule adds are delivered through the platform path
(``events.platform``). For the routed thread a rule can only drop the delivery
(its writer's own) or ride it with a note. Comments have no declared type yet,
so no rule matches them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from events.connectors.github import webhook_rule_envelope
from events.github_mapper import ends_artifact, manifest_type
from events.platform import apply_webhook_rules_sync, record_rule_ride_sync
from infra.product_events import emit_product_event

if TYPE_CHECKING:
    from events.connectors.github_route_preparation import PreparedRoute


def apply_routed_rules(
    prepared: PreparedRoute, internal_event: str, payload: dict[str, Any],
    check_suite_base_ref: str | None,
) -> None:
    """Deliver what rules add, then drop or annotate the routed delivery in ``prepared``."""
    event_type = manifest_type(internal_event)
    if event_type is None or prepared.thread_id is None:
        return
    envelope = webhook_rule_envelope(event_type, payload, prepared.identity, check_suite_base_ref)
    if envelope is None:
        return
    plan = apply_webhook_rules_sync(
        envelope, internal_event, prepared.thread_id, ends_artifact=ends_artifact(internal_event))
    if plan is None:
        return
    thread = prepared.thread_id
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
