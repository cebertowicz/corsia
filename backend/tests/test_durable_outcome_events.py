"""Permanent correlated durable-outcome instrumentation contracts."""

from __future__ import annotations

import pytest


def test_durable_outcome_uses_one_safe_correlation_envelope(monkeypatch) -> None:
    from infra import durable_outcome_events

    captured: list[tuple[str, str, str | None, dict]] = []
    monkeypatch.setattr(
        durable_outcome_events,
        "emit_product_event",
        lambda category, action, *, thread_id=None, properties=None, **_kwargs: (
            captured.append((category, action, thread_id, properties or {}))
        ),
    )

    durable_outcome_events.emit_durable_outcome(
        "delivery",
        "cold_recovery_succeeded",
        thread_id="session-1",
        turn_id="turn-1",
        message_id="message-1",
        operation_id="operation-1",
        completion_id="completion-1",
        reason="historical_image_warm_rejected",
        harness="codex",
        intent="apply_now",
        safe_fallback_latency_ms=17,
        attempt=2,
        revision=4,
        arbitration="winner",
    )

    assert captured == [
        (
            "durable_outcome",
            "delivery",
            "session-1",
            {
                "schema_version": 1,
                "signal": "delivery",
                "outcome": "cold_recovery_succeeded",
                "reason": "historical_image_warm_rejected",
                "correlation": {
                    "thread_id": "session-1",
                    "turn_id": "turn-1",
                    "message_id": "message-1",
                    "operation_id": "operation-1",
                    "completion_id": "completion-1",
                },
                "dimensions": {
                    "harness": "codex",
                    "intent": "apply_now",
                    "arbitration": "winner",
                },
                "measurement": {
                    "safe_fallback_latency_ms": 17,
                    "attempt": 2,
                    "revision": 4,
                },
            },
        )
    ]
    flattened = repr(captured[0][3]).lower()
    for forbidden in ("bytes", "prompt", "path", "token", "secret"):
        assert forbidden not in flattened


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("safe_fallback_latency_ms", -1),
        ("attempt", 0),
        ("revision", 0),
        ("thread_id", ""),
        ("reason", {"prompt": "secret"}),
    ],
)
def test_durable_outcome_rejects_unsafe_or_invalid_values(field: str, value) -> None:
    from infra.durable_outcome_events import emit_durable_outcome

    kwargs = {"thread_id": "session-1", field: value}
    with pytest.raises((TypeError, ValueError)):
        emit_durable_outcome("delivery", "failed", **kwargs)


def test_durable_outcome_omits_absent_optional_fields(monkeypatch) -> None:
    from infra import durable_outcome_events

    captured: list[dict] = []
    monkeypatch.setattr(
        durable_outcome_events,
        "emit_product_event",
        lambda _category, _action, **kwargs: captured.append(kwargs["properties"]),
    )
    durable_outcome_events.emit_durable_outcome(
        "malformed_head",
        "rescheduled",
        thread_id="session-2",
        revision=3,
    )

    assert captured[0]["correlation"] == {"thread_id": "session-2"}
    assert captured[0]["dimensions"] == {}
    assert captured[0]["measurement"] == {"revision": 3}
    assert "reason" not in captured[0]
