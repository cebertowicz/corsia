"""Pure global admission, fairness, deferral, and jitter tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from automation.continuation import admission
from automation.continuation.triggers import Trigger


def _trigger(
    trigger_id: str,
    scope: str,
    *,
    deferrals: int = 0,
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> Trigger:
    return Trigger(
        id=trigger_id,
        kind="t0_liveness",
        thread_id=f"thread-{trigger_id}",
        scope_id=scope,
        subject_id=None,
        dedupe_key=f"key-{trigger_id}",
        idempotency_key=None,
        state="pending",
        reason=None,
        attempt=0,
        deferrals=deferrals,
        cost_estimate_usd=0.0,
        next_attempt_at=None,
        created_at=created_at,
        updated_at=created_at,
    )


def test_global_in_flight_and_scope_cost_defer_first_match(monkeypatch):
    monkeypatch.setattr(admission, "MAX_GLOBAL_INFLIGHT", 4)
    monkeypatch.setattr(admission, "SCOPE_COST_CEILING_USD", 5.0)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    global_decision = admission.decide(
        _trigger("global", "a"),
        admission.AdmissionSnapshot(4, {"a": 0.0}, {}),
        now,
        admitted_by_scope={},
        admitted_total=0,
    )
    cost_decision = admission.decide(
        _trigger("cost", "a"),
        admission.AdmissionSnapshot(0, {"a": 5.0}, {}),
        now,
        admitted_by_scope={},
        admitted_total=0,
    )

    assert (global_decision.action, global_decision.reason) == (
        "defer",
        "global_in_flight",
    )
    assert (cost_decision.action, cost_decision.reason) == (
        "defer",
        "scope_cost_ceiling",
    )


def test_round_robin_fairness_across_three_scopes():
    batch = [
        _trigger("a1", "a"),
        _trigger("a2", "a"),
        _trigger("b1", "b"),
        _trigger("b2", "b"),
        _trigger("c1", "c"),
        _trigger("c2", "c"),
    ]
    snapshot = admission.AdmissionSnapshot(
        0,
        {},
        {
            "b": "2025-01-01T00:00:00+00:00",
            "c": "2025-02-01T00:00:00+00:00",
            "a": "2025-03-01T00:00:00+00:00",
        },
    )

    ordered = admission.fair_order(batch, snapshot)

    assert [trigger.id for trigger in ordered] == [
        "b1",
        "c1",
        "a1",
        "b2",
        "c2",
        "a2",
    ]


def test_deferral_is_bounded_and_jitter_is_deterministic(monkeypatch):
    monkeypatch.setattr(admission, "MAX_GLOBAL_INFLIGHT", 0)
    monkeypatch.setattr(admission, "MAX_DEFERRALS", 5)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snapshot = admission.AdmissionSnapshot(0, {}, {})

    terminal = admission.decide(
        _trigger("done", "a", deferrals=5),
        snapshot,
        now,
        admitted_by_scope={},
        admitted_total=0,
    )
    first = admission.deferred_until("stable", 1, now)
    second = admission.deferred_until("stable", 1, now)
    other = admission.deferred_until("other", 1, now)

    assert (terminal.action, terminal.reason) == ("stall", "deferral_exhausted")
    assert first == second
    assert first != other
    assert first > now


@pytest.mark.parametrize("thread_id", ["a", "b", "c"])
def test_restart_jitter_is_stable_and_bounded(thread_id):
    value = admission.restart_jitter_seconds(thread_id, maximum=30)
    assert value == admission.restart_jitter_seconds(thread_id, maximum=30)
    assert 0 <= value <= 30
