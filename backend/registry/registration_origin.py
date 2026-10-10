"""Trusted registration-origin validation and attribution."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from registry.identity_bridge import OriginRebind

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegistrationOrigin:
    """Trusted, server-issued identity of the Turn producing a registration."""

    thread_id: str
    turn_id: str
    producer: str


def origin_from_tool_context(
    thread_id: str,
    kwargs: dict[str, Any],
    producer: str,
) -> RegistrationOrigin | None:
    """Build origin only from the server-injected Turn identity."""
    identity = kwargs.get("_host_access_turn_identity")
    turn_id = getattr(identity, "turn_id", None)
    if not isinstance(turn_id, str) or not turn_id:
        return None
    return RegistrationOrigin(thread_id, turn_id, producer)


def resolve_rebind(db, parent_thread_id: str, origin: RegistrationOrigin | None):
    """Resolve a registration origin to its admissible plan-node attribution."""
    if origin is None:
        return OriginRebind(None), None
    if origin.thread_id != parent_thread_id:
        reason = "origin_thread_mismatch"
        log.warning(
            "registry origin rejected: reason=%s producer=%s",
            reason,
            origin.producer,
        )
        return OriginRebind(None), {
            "origin_outcome": "unbound",
            "origin_reason": reason,
            "producer": origin.producer,
            "node_state": None,
            "usage_role": None,
        }
    from plans.public import registration_origin

    resolved = registration_origin(origin.thread_id, origin.turn_id, db=db)
    if resolved.reason in {
        "turn_unknown",
        "turn_thread_mismatch",
        "origin_thread_mismatch",
    }:
        log.warning(
            "registry origin rejected: reason=%s producer=%s",
            resolved.reason,
            origin.producer,
        )
    return OriginRebind(resolved.node_id), {
        "origin_outcome": resolved.outcome,
        "origin_reason": resolved.reason,
        "producer": origin.producer,
        "node_state": resolved.node_state,
        "usage_role": resolved.usage_role,
    }
