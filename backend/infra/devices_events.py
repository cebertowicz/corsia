"""The one Devices product-event emitter, shared by the Devices API and listener."""

from __future__ import annotations

from infra.product_events import emit_product_event


def emit_devices_event(action: str, *, reason: str | None = None) -> None:
    """Record one Devices product event; ``reason`` is its only property."""
    properties = {"reason": reason} if reason is not None else None
    emit_product_event("devices", action, properties=properties)


def emit_pairing_failure(*, reason: str) -> None:
    """Record a pairing refusal without coupling infrastructure to HTTP routes."""
    emit_devices_event("pairing_failed", reason=reason)
