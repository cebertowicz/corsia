"""The single owner-resolution path for Event Delivery (coordination MVP S4).

``resolve_targets`` is THE routing-decision function the §1 diagram describes. It
handles exactly five outcomes and names no connector:

1. **registered artifact resolve** — the artifact has a live owner (PATH A).
2. **declaration-bind + register** — first-seen identity bound by a
   connector-validated declaration in the connector-agnostic binder.
3. **rule-bind + register** — first-seen identity bound+owned by a Connections
   rule (PATH B), delegated to the connector-agnostic binder.
4. **ownerless triage** — an ``ownerless_ok`` event with no owner.
5. **dead-letter** — no artifact / no owner and not ownerless.

S4a built the resolution; S4b attaches the **disposition** (wake|digest) from the
disposition table and the **live health sink** for failure/dead-letter outcomes.
``delivery_failure`` (mechanism fault, watch-expiry) is kept DISTINCT from
``dead_letter`` (no owner). The caller still owns delivery and dead-letter
parking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from connections import binder
from events import delivery, health_sink
from events.disposition import delivery_disposition as _disp
from events.envelope import EventEnvelope
from registry import models as registry_models


@dataclass
class Target:
    """One resolved recipient + how it was resolved + how it should be delivered."""

    thread: str | None
    resolution: str                       # path_a_resolve|path_b_declaration_bind|path_b_rule_bind|triage|dead_letter|repair|delivery_failure
    relationship: str                     # owner | triage | repair | none
    reason: str | None = None
    routing_state: dict[str, Any] | None = None
    repair_artifact: Any = None
    disposition: str | None = None        # wake | digest (from the disposition table)
    health_owner: str | None = None       # live health sink for failure/dead-letter
    health_sink_is_live: bool | None = None


def _routing(ref: dict, db=None) -> dict:
    return registry_models.routing_registrations(
        ref["integration"], ref["type"], ref["external_id"], db=db)


def _lifecycle(routing_state: dict | None) -> str:
    identity = (routing_state or {}).get("identity")
    return identity["state"] if identity else "active"


def _deliverable(thread: str, relationship: str, routing_state: dict,
                 event_class: str) -> Target:
    """A target with a computed wake/digest disposition."""
    return Target(
        thread, _resolution_for(relationship), relationship,
        routing_state=routing_state,
        disposition=_disp(relationship, event_class, _lifecycle(routing_state)))


def _resolution_for(relationship: str) -> str:
    return {"owner": "path_a_resolve", "triage": "triage",
            "repair": "repair"}.get(relationship, "path_a_resolve")


def _dead_letter(reason: str, routing_state: dict | None, *, db=None) -> Target:
    """A no-owner dead-letter whose terminus is a guaranteed-live health sink."""
    sink = health_sink.resolve_live_health_owner(
        _triage_chain(routing_state), db=db)
    return Target(None, "dead_letter", "none", reason=reason,
                  routing_state=routing_state, disposition="dead_letter",
                  health_owner=sink, health_sink_is_live=True)


def _triage_chain(routing_state: dict | None) -> list[str | None]:
    """Narrowest-first candidate health owners; the resolver appends the sink."""
    triage = (routing_state or {}).get("triage")
    return [triage["parent_thread_id"] if triage else None]



def resolve_targets(
    envelope: EventEnvelope, *, repair_owner: str | None = None, db=None
) -> list[Target]:
    """Resolve a normalized envelope to its recipient target(s)."""
    ref = envelope.artifact_ref
    if ref is None:
        return [_dead_letter("no_artifact", None, db=db)]

    routing_state = _routing(ref, db=db)
    owner = routing_state.get("owner")
    triage = routing_state.get("triage")
    ec = envelope.event_class

    if owner is not None:
        thread = owner["parent_thread_id"]
        if delivery.thread_exists_and_active(thread, db=db):
            return [_deliverable(thread, "owner", routing_state, ec)]
        return [_dead_letter("owner_inactive", routing_state, db=db)]

    if triage is not None:
        thread = triage["parent_thread_id"]
        if delivery.thread_exists_and_active(thread, db=db):
            return [_deliverable(thread, "triage", routing_state, ec)]
        return [_dead_letter("owner_inactive", routing_state, db=db)]

    # No registered owner — try connector-agnostic declaration then rule binding.
    bind = binder.bind_owner_for(
        ref, envelope.facts, envelope.connector, repair_owner=repair_owner, db=db)

    if bind.via in ("declaration", "rule", "ownership"):
        resolution = {
            "declaration": "path_b_declaration_bind",
            "rule": "path_b_rule_bind",
            "ownership": "path_a_resolve",
        }[bind.via]
        rs = _routing(ref, db=db)
        return [Target(bind.record.owner_thread, resolution, "owner",
                       routing_state=rs,
                       disposition=_disp("owner", ec, _lifecycle(rs)))]

    if bind.via == "repair":
        repair_thread = bind.repair_artifact.owner_thread if bind.repair_artifact else None
        return [Target(repair_thread, "repair", "repair", reason="rule_conflict",
                       repair_artifact=bind.repair_artifact,
                       routing_state=routing_state,
                       disposition=_disp("repair", ec))]

    # Unbound: ownerless events await a triage owner; everything else dead-letters.
    if envelope.ownerless_ok:
        return [Target(None, "triage", "triage",
                       reason="ownerless_awaiting_triage_owner",
                       routing_state=routing_state,
                       disposition=_disp("triage", ec))]
    return [_dead_letter("no_owner", routing_state, db=db)]


def resolve_delivery_failure(
    owner_thread: str | None, *, reason: str, db=None
) -> Target:
    """Route a mechanism fault / missing-expected-event as a delivery failure.

    Distinct from a no-owner dead-letter: a known owner is woken to investigate;
    if the owner is non-live the failure still reaches a live health sink, never a
    silent dead-end. Automation owns the watch lifecycle (S7); this only routes.
    """
    sink = health_sink.resolve_live_health_owner([owner_thread], db=db)
    return Target(sink, "delivery_failure", "owner", reason=reason,
                  disposition="owner_wake", health_owner=sink,
                  health_sink_is_live=True)
