"""System causes a Stop can carry into a Turn's terminal (#2477 amendment 12)."""

from __future__ import annotations

PLAN_LIMIT_REASON = "plan_limit_overage_off"
#: A harness Turn the vendor refused for a usage limit (the account is recorded limited).
VENDOR_LIMIT_REFUSAL_REASON = "vendor_usage_limit_refusal"
#: A harness Turn the provider refused because its context outgrew the window.
CONTEXT_OVERFLOW_REASON = "context_overflow.continue"


class PlanLimitStop(Exception):
    """A system Stop at the plan limit: its inputs return to the queue, not cancelled."""

    cause = PLAN_LIMIT_REASON

    def __init__(self) -> None:
        super().__init__(PLAN_LIMIT_REASON)


class ContextOverflowEnd(Exception):
    """A harness Turn the provider refused for size: it ends ``error`` and runs once more.

    The harness has already dropped the provider session, so the successor is
    prepared cold from CORSIA's history and its boundary folds what no longer fits.
    """

    cause = CONTEXT_OVERFLOW_REASON


def frame_cause(error: object) -> dict[str, str]:
    """The ``turn_cancelled`` frame field for a system cause; empty for a user Stop."""
    return {"cause": error.cause} if isinstance(error, PlanLimitStop) else {}
