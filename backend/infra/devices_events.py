"""Product-event helpers shared by Devices API and listener paths."""

from __future__ import annotations

from infra.product_events import emit_product_event


def emit_pairing_failure(*, reason: str) -> None:
    """Record a pairing refusal without coupling infrastructure to HTTP routes."""
    emit_product_event("devices", "pairing_failed", properties={"reason": reason})
