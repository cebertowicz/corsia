"""Closed continuation vocabulary and durable queue value objects."""

from __future__ import annotations

from dataclasses import dataclass

TERMINAL_STATES = frozenset(
    {"delivered", "suppressed", "stalled", "cancelled", "failed"}
)
LIVE_STATES = frozenset({"pending", "dispatched", "deferred"})


class StaleContinuation(RuntimeError):
    """A collected plan wake whose source no longer permits delivery."""


REASONS = frozenset(
    {
        "progress_advanced",
        "progress_churned",
        "progress_unchanged",
        "no_actionable_work",
        "watch_armed",
        "watch_expired",
        "watch_resolved",
        "watchdog_due",
        "cycle_cap_reached",
        "wake_budget_exhausted",
        "deferral_exhausted",
        "scope_cost_ceiling",
        "scope_fairness",
        "global_in_flight",
        "thread_unavailable",
        "delivery_failed",
        "budget_reset",
        "schedule_due",
        "schedule_exhausted",
        "restart_resume",
        "restart_unresolved",
        "source_retired",
        "plan_paused",
        "stale_source_state",
    }
)


@dataclass(frozen=True)
class TriggerRequest:
    """One source request before durable dedupe."""

    kind: str
    thread_id: str
    scope_id: str
    subject_id: str | None
    transition: str
    cost_estimate_usd: float = 0.0
    outcome: str | None = None
    reason: str | None = None
    details: dict | None = None
    not_before: str | None = None

    @property
    def dedupe_key(self) -> str:
        """Return the canonical source/subject/transition identity."""

        return (
            f"{self.kind}:{self.thread_id}:"
            f"{self.subject_id or '-'}:{self.transition}"
        )


@dataclass(frozen=True)
class Trigger:
    """One durable continuation queue row."""

    id: str
    kind: str
    thread_id: str
    scope_id: str
    subject_id: str | None
    dedupe_key: str
    idempotency_key: str | None
    state: str
    reason: str | None
    attempt: int
    deferrals: int
    cost_estimate_usd: float
    next_attempt_at: str | None
    created_at: str
    updated_at: str
