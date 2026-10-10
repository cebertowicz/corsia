"""Permanent safe correlation envelope for durable lifecycle outcomes.

The product-event store remains non-load-bearing. This module only standardizes
an allowlisted diagnostic shape; it never owns lifecycle state and deliberately
has no free-form properties channel.
"""

from __future__ import annotations

from typing import Literal

from infra.product_events import emit_product_event

Arbitration = Literal["winner", "loser"]


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _positive_int(value: object, field: str, *, allow_zero: bool = False) -> int | None:
    if value is None:
        return None
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{field} must be a {qualifier} integer")
    return value


def emit_durable_outcome(
    signal: str,
    outcome: str,
    *,
    thread_id: str,
    turn_id: str | None = None,
    message_id: str | None = None,
    operation_id: str | None = None,
    completion_id: str | None = None,
    reason: str | None = None,
    harness: str | None = None,
    intent: str | None = None,
    safe_fallback_latency_ms: int | None = None,
    attempt: int | None = None,
    revision: int | None = None,
    arbitration: Arbitration | None = None,
) -> None:
    """Emit one identifier-only durable outcome through product analytics."""
    normalized_signal = _required_text(signal, "signal")
    normalized_outcome = _required_text(outcome, "outcome")
    normalized_thread_id = _required_text(thread_id, "thread_id")
    normalized_reason = _optional_text(reason, "reason")
    normalized_arbitration = _optional_text(arbitration, "arbitration")
    if normalized_arbitration not in {None, "winner", "loser"}:
        raise ValueError("arbitration must be winner or loser")

    correlation = {
        key: value
        for key, value in {
            "thread_id": normalized_thread_id,
            "turn_id": _optional_text(turn_id, "turn_id"),
            "message_id": _optional_text(message_id, "message_id"),
            "operation_id": _optional_text(operation_id, "operation_id"),
            "completion_id": _optional_text(completion_id, "completion_id"),
        }.items()
        if value is not None
    }
    dimensions = {
        key: value
        for key, value in {
            "harness": _optional_text(harness, "harness"),
            "intent": _optional_text(intent, "intent"),
            "arbitration": normalized_arbitration,
        }.items()
        if value is not None
    }
    measurement = {
        key: value
        for key, value in {
            "safe_fallback_latency_ms": _positive_int(
                safe_fallback_latency_ms,
                "safe_fallback_latency_ms",
                allow_zero=True,
            ),
            "attempt": _positive_int(attempt, "attempt"),
            "revision": _positive_int(revision, "revision"),
        }.items()
        if value is not None
    }
    properties: dict[str, object] = {
        "schema_version": 1,
        "signal": normalized_signal,
        "outcome": normalized_outcome,
        "correlation": correlation,
        "dimensions": dimensions,
        "measurement": measurement,
    }
    if normalized_reason is not None:
        properties["reason"] = normalized_reason
    emit_product_event(
        "durable_outcome",
        normalized_signal,
        thread_id=normalized_thread_id,
        properties=properties,
    )
